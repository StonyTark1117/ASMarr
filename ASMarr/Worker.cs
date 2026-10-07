using Microsoft.AspNetCore.SignalR;
using System.Text.Json;

namespace ASMarr;
public sealed class StatusHub:Hub {}

public sealed class Worker(Store store,ProviderProcess providers,IHubContext<StatusHub> hub,ILogger<Worker> logger):BackgroundService
{
    readonly string owner=Guid.NewGuid().ToString("N");
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        // A terminated process cannot resume an executing command. Retain an
        // interrupted record for explicit retry rather than replaying a mutation.
        store.Execute("UPDATE commands SET state='interrupted',finished=$now,error='Service restarted during execution' WHERE state='running'",("now",DateTimeOffset.UtcNow.ToString("O")));
        if(store.Query("SELECT key FROM assets LIMIT 1").Count==0&&File.Exists(System.IO.Path.Combine(store.ConfigRoot,"sources.yaml"))&&store.Query("SELECT id FROM commands WHERE name='migration' AND state='queued'").Count==0)
            store.Enqueue("migration",new {});
        while(!stoppingToken.IsCancellationRequested)
        {
            try
            {
                var now=DateTimeOffset.UtcNow.ToString("O");
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
        int locked=store.Execute("INSERT INTO task_locks(name,owner,expires) VALUES('providers',$owner,$expiry) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires=excluded.expires WHERE task_locks.expires<$now",("owner",owner),("expiry",now.AddMinutes(2).ToString("O")),("now",now.ToString("O")));
        if(locked==0)return;
        using var leaseCancel=CancellationTokenSource.CreateLinkedTokenSource(ct);
        var lease=Task.Run(async()=> { while(!leaseCancel.IsCancellationRequested) { await Task.Delay(30000,leaseCancel.Token); store.Execute("UPDATE task_locks SET expires=$expiry WHERE name='providers' AND owner=$owner",("expiry",DateTimeOffset.UtcNow.AddMinutes(2).ToString("O")),("owner",owner)); } },leaseCancel.Token);
        store.Execute("UPDATE commands SET state='running',started=$now WHERE id=$id AND state='queued'",("id",id),("now",now.ToString("O")));
        await hub.Clients.All.SendAsync("status",new {id,name,state="running"},ct);
        try
        {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);timeout.CancelAfter(TimeSpan.FromMinutes(30));
            using var args=JsonDocument.Parse(c["arguments"]!.ToString()!);
            bool shadow=store.Setting("mode","shadow")!="production";
            if(name=="discovery"&&store.Query("SELECT id FROM migration_audits LIMIT 1").Count==0)throw new InvalidOperationException("Complete the migration audit before discovery");
            object result=name switch
            {
                "migration" => await providers.Run("migrate",new {},timeout.Token),
                "discovery" => await providers.Run("discover",new {shadow,kind=args.RootElement.TryGetProperty("kind",out var k)?k.GetString():"all"},timeout.Token),
                "disk-scan" => await providers.Run("scan",new {},timeout.Token),
                "plex-verify" => await providers.Run("plex-verify",new {},timeout.Token),
                "plex" => shadow ? new {status="shadow",message="Plex writes disabled"} : (object)await providers.Run("plex",new {},timeout.Token),
                "playlists" => await providers.Run("playlists",new {preview=shadow},timeout.Token),
                "queue" => shadow ? new {status="shadow",message="Acquisitions disabled"} : (object)await providers.Run("process-queue",new {},timeout.Token),
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
        }
        catch(Exception e)
        {
            store.Execute("UPDATE commands SET state='failed',error=$error,finished=$now WHERE id=$id",("id",id),("error",e.Message),("now",DateTimeOffset.UtcNow.ToString("O")));
            store.Log("error",name+": "+e.Message); logger.LogWarning("Command {Name} failed: {Type}",name,e.GetType().Name);
        }
        finally
        {
            leaseCancel.Cancel();try{await lease;}catch(OperationCanceledException){}
            store.Execute("DELETE FROM task_locks WHERE name='providers' AND owner=$owner",("owner",owner));
            await hub.Clients.All.SendAsync("status",new {id,name,state="finished"},ct);
        }
    }
}
