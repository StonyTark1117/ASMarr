using Microsoft.AspNetCore.SignalR;
using System.Text.Json;

namespace ASMarr;
public sealed class StatusHub:Hub {}

public sealed class Worker(Store store,ProviderProcess providers,IHubContext<StatusHub> hub,INotificationProvider notifications,ILogger<Worker> logger):BackgroundService
{
    readonly string owner=Guid.NewGuid().ToString("N");
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        // A terminated process cannot resume an executing command. Backfill
        // checkpoints are safe to requeue; other interrupted mutations remain
        // available for explicit review.
        store.Execute("UPDATE commands SET state='interrupted',finished=$now,error='Service restarted during execution' WHERE state='running'",("now",DateTimeOffset.UtcNow.ToString("O")));
        store.InterruptRunningVideoBackfills();
        var nextBackfillRecovery=DateTimeOffset.MinValue;
        if(store.Query("SELECT key FROM assets LIMIT 1").Count==0&&File.Exists(System.IO.Path.Combine(store.ConfigRoot,"sources.yaml"))&&store.Query("SELECT id FROM commands WHERE name='migration' AND state='queued'").Count==0)
            store.Enqueue("migration",new {});
        while(!stoppingToken.IsCancellationRequested)
        {
            try
            {
                var now=DateTimeOffset.UtcNow.ToString("O");
                if(DateTimeOffset.UtcNow>=nextBackfillRecovery)
                {
                    store.EnqueueResumableVideoBackfills(TimeSpan.FromMinutes(5));
                    nextBackfillRecovery=DateTimeOffset.UtcNow.AddMinutes(1);
                }
                foreach(var t in store.Query("SELECT * FROM tasks WHERE enabled=1 AND next_run<=$now",("now",now)))
                {
                    string name=t["name"]!.ToString()!;
                    if(store.Query("SELECT id FROM commands WHERE name=$name AND state IN ('queued','running')",("name",name)).Count==0) store.Enqueue(name,new {});
                    store.Execute("UPDATE tasks SET next_run=$next WHERE name=$name",("name",name),("next",DateTimeOffset.UtcNow.AddSeconds(Convert.ToInt32(t["interval_seconds"])).ToString("O")));
                }
                var command=store.Query("SELECT * FROM commands WHERE state='queued' ORDER BY created LIMIT 1").FirstOrDefault();
                if(command!=null) await ExecuteCommand(command,stoppingToken);
            }
            catch(OperationCanceledException) when(stoppingToken.IsCancellationRequested) { break; }
            catch(Exception e) { logger.LogError(e,"Worker loop failed");store.Log("error","Worker loop failed: "+e.GetType().Name); }
            await Task.Delay(TimeSpan.FromSeconds(2),stoppingToken);
        }
    }
    async Task ExecuteCommand(Dictionary<string,object?> c,CancellationToken ct)
    {
        string id=c["id"]!.ToString()!,name=c["name"]!.ToString()!;
        // All provider work shares a renewable database lease. Different task
        // names cannot race discovery, transfer publication or Plex mutations.
        var now=DateTimeOffset.UtcNow;
        if(!store.TryAcquireLease("providers",owner,TimeSpan.FromMinutes(2)))return;
        using var leaseCancel=CancellationTokenSource.CreateLinkedTokenSource(ct);
        var lease=Task.Run(async()=> { while(!leaseCancel.IsCancellationRequested) { await Task.Delay(30000,leaseCancel.Token); if(!store.RenewLease("providers",owner,TimeSpan.FromMinutes(2))){leaseCancel.Cancel();return;} } },leaseCancel.Token);
        if(store.Execute("UPDATE commands SET state='running',started=$now WHERE id=$id AND state='queued'",("id",id),("now",now.ToString("O")))!=1)
        {
            store.ReleaseLease("providers",owner);leaseCancel.Cancel();try{await lease;}catch(OperationCanceledException){}return;
        }
        await hub.Clients.All.SendAsync("status",new {id,name,state="running"},ct);
        try
        {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(leaseCancel.Token);timeout.CancelAfter(TimeSpan.FromMinutes(30));
            using var args=JsonDocument.Parse(c["arguments"]!.ToString()!);
            bool shadow=store.Setting("mode","shadow")!="production";
            if(name=="discovery"&&store.Query("SELECT id FROM migration_audits LIMIT 1").Count==0)throw new InvalidOperationException("Complete the migration audit before discovery");
            object result=name switch
            {
                "migration" => await providers.Run("migrate",new {},timeout.Token),
                "discovery" => await providers.Run("discover",new {shadow,kind=args.RootElement.TryGetProperty("kind",out var k)?k.GetString():"all"},timeout.Token),
                "video-backfill" => await RunVideoBackfill(args.RootElement,id,timeout.Token),
                "disk-scan" => await providers.Run("scan",new {},timeout.Token),
                "plex-verify" => await providers.Run("plex-verify",new {},timeout.Token),
                "playlists-verify" => await providers.Run("playlists-verify",new {},timeout.Token),
                "plex" => shadow ? new {status="shadow",message="Plex writes disabled"} : (object)await providers.Run("plex",new {},timeout.Token),
                "plex-video-verify" => await providers.Run("plex-video-verify",new {},timeout.Token),
                "plex-video" => shadow ? new {status="shadow",message="Plex video writes disabled"} : (object)await providers.Run("plex-video",new {},timeout.Token),
                "playlists" => await providers.Run("playlists",new {preview=shadow},timeout.Token),
                "queue" => shadow ? new {status="shadow",message="Acquisitions disabled"} : (object)await providers.Run("process-queue",new {},timeout.Token),
                "video-queue" => shadow ? new {status="shadow",message="Video acquisitions disabled"} : (object)await providers.Run("video-process-queue",new {},timeout.Token),
                "health" => new {status="ok",integrity=store.Query("PRAGMA quick_check"),mode=store.Setting("mode")},
                "backup" => new {path=store.Backup()},
                "source-test" => await providers.Run("test-source",args.RootElement,timeout.Token),
                "search" => await providers.Run("search",args.RootElement,timeout.Token),
                "grab" => await providers.Run("grab",args.RootElement,timeout.Token),
                "manual-import" => await providers.Run("manual-import",args.RootElement,timeout.Token),
                "rename-preview" => await providers.Run("rename-preview",args.RootElement,timeout.Token),
                "integration-test" => await providers.Run("integration-test",args.RootElement,timeout.Token),
                _ => throw new InvalidOperationException("Unsupported command")
            };
            string json=JsonSerializer.Serialize(result);
            store.Execute("UPDATE commands SET state='completed',result=$result,finished=$now WHERE id=$id",("id",id),("result",json),("now",DateTimeOffset.UtcNow.ToString("O")));
            store.Execute("UPDATE tasks SET last_run=$now,last_result=$result WHERE name=$name",("name",name),("now",DateTimeOffset.UtcNow.ToString("O")),("result",json));
            store.Log("info",name+" completed");
            if(name is "video-backfill" or "video-queue")await hub.Clients.All.SendAsync("assetState",new {mediaKind="Video",command=id,state="updated"},ct);
            if(name=="video-queue"&&result is JsonElement videoResult&&videoResult.TryGetProperty("jobs",out var videoJobs))
                foreach(var job in videoJobs.EnumerateArray())
                    await hub.Clients.All.SendAsync("assetState",new {mediaKind="Video",command=id,
                        recordingKey=job.TryGetProperty("key",out var videoKey)?videoKey.GetString():null,
                        state=job.TryGetProperty("status",out var videoState)?videoState.GetString():"updated"},ct);
            if(name=="queue"&&result is JsonElement mediaQueueResult&&mediaQueueResult.TryGetProperty("downloads",out var downloads)&&
               downloads.TryGetProperty("jobs",out var downloadJobs))
                foreach(var job in downloadJobs.EnumerateArray())
                    if(job.TryGetProperty("mediaKind",out var mediaKind))
                        await hub.Clients.All.SendAsync("assetState",new {mediaKind=mediaKind.GetString(),command=id,
                            recordingKey=job.TryGetProperty("key",out var downloadKey)?downloadKey.GetString():null,
                            state=job.TryGetProperty("status",out var downloadState)?downloadState.GetString():"updated"},ct);
            if(name=="grab"&&result is JsonElement grabResult&&grabResult.TryGetProperty("mediaKind",out var grabbedKind))
                await hub.Clients.All.SendAsync("assetState",new {mediaKind=grabbedKind.GetString(),command=id,
                    recordingKey=grabResult.TryGetProperty("key",out var grabbedKey)?grabbedKey.GetString():null,
                    state=grabResult.TryGetProperty("status",out var grabbedState)?grabbedState.GetString():"updated"},ct);
            if(name=="queue"&&!shadow&&result is JsonElement queueResult&&queueResult.TryGetProperty("saved",out var saved)&&saved.GetArrayLength()>0)
            {
                try{await notifications.Notify($"ASMarr imported {saved.GetArrayLength()} recordings.",ct);}catch(Exception){store.Log("warning","Import succeeded; notification delivery failed");}
            }
        }
        catch(Exception e)
        {
            store.Execute("UPDATE commands SET state='failed',error=$error,finished=$now WHERE id=$id",("id",id),("error",e.Message),("now",DateTimeOffset.UtcNow.ToString("O")));
            store.Log("error",name+": "+e.Message); logger.LogWarning("Command {Name} failed: {Type}",name,e.GetType().Name);
            if(name is "queue" or "discovery")try{await notifications.Notify($"ASMarr {name} failed. Check System tasks for details.",ct);}catch(Exception){store.Log("warning","Failure notification delivery failed");}
        }
        finally
        {
            leaseCancel.Cancel();try{await lease;}catch(OperationCanceledException){}
            store.ReleaseLease("providers",owner);
            await hub.Clients.All.SendAsync("status",new {id,name,state="finished"},ct);
        }
    }
    async Task<JsonElement> RunVideoBackfill(JsonElement arguments,string commandId,CancellationToken ct)
    {
        int creatorId=arguments.GetProperty("creatorId").GetInt32();
        Dictionary<string,string?> observed=store.Query("SELECT m.recording_key,m.state FROM media_assets m JOIN assets a ON a.key=m.recording_key WHERE m.media_kind='Video' AND a.creator=(SELECT name FROM creators WHERE id=$creator)",("creator",creatorId))
            .ToDictionary(row=>row["recording_key"]!.ToString()!,row=>row["state"]?.ToString());
        async Task PublishAssetChanges()
        {
            foreach(var row in store.Query("SELECT m.recording_key,m.state FROM media_assets m JOIN assets a ON a.key=m.recording_key WHERE m.media_kind='Video' AND a.creator=(SELECT name FROM creators WHERE id=$creator)",("creator",creatorId)))
            {
                string key=row["recording_key"]!.ToString()!,state=row["state"]?.ToString()??"updated";
                if(!observed.TryGetValue(key,out var previous)||previous!=state)
                {
                    observed[key]=state;
                    await hub.Clients.All.SendAsync("assetState",new {mediaKind="Video",command=commandId,recordingKey=key,state},ct);
                }
            }
        }
        var run=providers.Run("video-backfill",arguments,ct);string? previous=null;
        while(!run.IsCompleted)
        {
            var job=store.Query("SELECT * FROM backfill_jobs WHERE creator_id=$creator AND media_kind='Video' ORDER BY started DESC LIMIT 1",("creator",creatorId)).FirstOrDefault();
            if(job!=null)
            {
                string current=JsonSerializer.Serialize(job);
                if(current!=previous)
                {
                    previous=current;
                    await hub.Clients.All.SendAsync("backfillProgress",new {mediaKind="Video",command=commandId,creatorId,job},ct);
                }
            }
            await PublishAssetChanges();
            await Task.WhenAny(run,Task.Delay(500,ct));
        }
        var result=await run;
        await PublishAssetChanges();
        await hub.Clients.All.SendAsync("backfillProgress",new {mediaKind="Video",command=commandId,creatorId,result},ct);
        return result;
    }
}
