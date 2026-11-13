using ASMarr;
using Microsoft.Data.Sqlite;

var root=Path.Combine(Path.GetTempPath(),"asmarr-store-tests-"+Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(root);
Environment.SetEnvironmentVariable("ASMARR_STATE",Path.Combine(root,"state"));
Environment.SetEnvironmentVariable("ASMARR_CONFIG",Path.Combine(root,"config"));
var store=new Store();store.Initialize();store.Initialize();
int assertions=0;
void Assert(bool condition,string name){if(!condition)throw new InvalidOperationException(name);assertions++;Console.WriteLine("PASS "+name);}
Assert(WebDeployment.UrlBase(null)==""&&WebDeployment.UrlBase("/")=="","default URL base remains root");
Assert(WebDeployment.UrlBase("/apps/asmarr")=="/apps/asmarr","nested URL base is supported");
foreach(var invalid in new[]{"asmarr","/asmarr/","//host","/../escape","/x?query","/x\"onclick"})
{
    bool rejected=false;try{WebDeployment.UrlBase(invalid);}catch(InvalidOperationException){rejected=true;}
    Assert(rejected,"invalid URL base is rejected: "+invalid);
}
Assert(store.Query("PRAGMA integrity_check")[0].Values.First()!.ToString()=="ok","migrations retain database integrity");
Assert(store.Query("PRAGMA journal_mode")[0].Values.First()!.ToString()=="wal","database uses WAL");
Assert(store.Query("SELECT * FROM schema_migrations").Count==3,"repeat migration is idempotent");
Assert(store.Query("SELECT * FROM tasks").Count==10,"repeat migration does not duplicate tasks");
Assert(store.Query("SELECT * FROM source_catalog").Count==0&&store.Query("SELECT * FROM source_previews").Count==0&&store.Query("SELECT * FROM source_suggestions").Count==0,"public source catalog tables initialize empty");
Assert(store.Query("SELECT interval_seconds FROM tasks WHERE name='source-suggest'")[0]["interval_seconds"]!.ToString()=="604800","source suggestions run weekly without linking sources");
Assert(store.Query("SELECT * FROM video_quality_profiles").Count==6,"built-in video profiles are seeded once");
Assert(store.Query("SELECT monitor_video FROM creators").Count==0,"migration does not invent creators or enable video");
Assert(store.Setting("mode")=="shadow","first boot gates production writes");
var deployedAt=new DateTimeOffset(2026,10,8,0,0,0,TimeSpan.Zero);
var sourceCommit=new string('a',40);var artifactHash=new string('b',64);
File.WriteAllText(Path.Combine(store.Root,"deployed-release-runtime.json"),System.Text.Json.JsonSerializer.Serialize(new {sourceCommit,artifactSha256=artifactHash,deployedAt}));
var parity=new Dictionary<string,object?> {
    ["sourceCommit"]=sourceCommit,["artifactSha256"]=artifactHash,["legacyImplementationSha256"]=new string('c',64),
    ["discoveryParity"]=true,["eligibilityAndSourceParity"]=true,["checkpointCalculationParity"]=true,
    ["productionCheckpointsUnchanged"]=true,["applicationCheckpointsUnchanged"]=true,["mediaUnchanged"]=true,
    ["playlistPreviewHealthy"]=true,["playlistCalculationParity"]=true
};
store.Execute("INSERT INTO shadow_cycles(started,finished,result,clean,comparison) VALUES($started,$started,'{}',1,$comparison)",("started","2026-10-08T01:00:00Z"),("comparison",System.Text.Json.JsonSerializer.Serialize(parity)));
store.Execute("INSERT INTO shadow_cycles(started,finished,result,clean,comparison) VALUES($started,$started,'{}',1,$comparison)",("started","2026-10-07T01:00:00Z"),("comparison",System.Text.Json.JsonSerializer.Serialize(parity)));
parity["sourceCommit"]=new string('d',40);
store.Execute("INSERT INTO shadow_cycles(started,finished,result,clean,comparison) VALUES($started,$started,'{}',1,$comparison)",("started","2026-10-09T01:00:00Z"),("comparison",System.Text.Json.JsonSerializer.Serialize(parity)));
store.Execute("INSERT INTO shadow_cycles(started,finished,result,clean,comparison) VALUES($started,$started,'{}',1,'not-json')",("started","2026-10-10T01:00:00Z"));
Assert(ShadowQualification.Count(store)==1,"status counts only current-release post-deployment shadow evidence");
var leases=await Task.WhenAll(Enumerable.Range(0,10).Select(i=>Task.Run(()=>store.TryAcquireLease("test-provider",i.ToString(),TimeSpan.FromMinutes(1)))));
Assert(leases.Count(x=>x)==1,"concurrent tasks have exactly one lease holder");
string owner=store.Query("SELECT owner FROM task_locks WHERE name='test-provider'")[0]["owner"]!.ToString()!;
Assert(!store.RenewLease("test-provider","wrong-owner",TimeSpan.FromMinutes(1)),"another owner cannot renew lease");
store.ReleaseLease("test-provider","wrong-owner");
Assert(!store.TryAcquireLease("test-provider","next",TimeSpan.FromMinutes(1)),"another owner cannot release lease");
Assert(store.RenewLease("test-provider",owner,TimeSpan.FromMinutes(1)),"lease holder renews active execution");
store.Execute("UPDATE task_locks SET expires=$expired WHERE name='test-provider'",("expired",DateTimeOffset.UtcNow.AddMinutes(-1).ToString("O")));
Assert(store.TryAcquireLease("test-provider","replacement",TimeSpan.FromMinutes(1)),"expired leases recover after interruption");
store.ReleaseLease("test-provider",owner);
Assert(store.Query("SELECT owner FROM task_locks WHERE name='test-provider'")[0]["owner"]!.ToString()=="replacement","former owner cannot release replacement lease");
var id=store.Enqueue("health",new {test=true});
var reopened=new Store();
Assert(reopened.Query("SELECT state FROM commands WHERE id=$id",("id",id))[0]["state"]!.ToString()=="queued","commands persist across connection lifetime");
string backup=store.Backup();
using(var db=new SqliteConnection("Data Source="+backup)){db.Open();using var c=db.CreateCommand();c.CommandText="SELECT count(*) FROM commands";Assert(Convert.ToInt32(c.ExecuteScalar())==1,"SQLite online backup retains durable commands");}
Console.WriteLine($"{assertions} store integration assertions passed");
store.Execute("INSERT INTO profiles VALUES(1,'Default','{}');INSERT INTO profiles VALUES(2,'Sleep','{}')");
store.Execute("INSERT INTO creators(id,name,path,tags) VALUES(1,'First','/fixture/first','[\"Existing\"]'),(2,'Second','/fixture/second','[]')");
Assert(CreatorBatch.Apply(store,new MassEdit([1,2],ProfileId:2,AddTags:["Sleep","sleep"])) is null,"mass edit applies valid profile and deduplicated tags");
Assert(store.Query("SELECT * FROM creators WHERE profile_id=2").Count==2,"mass profile edit persists for every selected creator");
Assert(store.Query("SELECT tags FROM creators WHERE id=1")[0]["tags"]!.ToString()=="[\"Existing\",\"Sleep\"]","additive tags preserve existing labels");
Assert(CreatorBatch.Apply(store,new MassEdit([1,999],Monitored:false)) is not null,"missing creator rejects the whole batch");
Assert(Convert.ToInt32(store.Query("SELECT monitored FROM creators WHERE id=1")[0]["monitored"])==1,"failed batch rolls back earlier creator changes");
Assert(CreatorBatch.Apply(store,new MassEdit([1],ProfileId:999)) is not null,"unknown profile is rejected without changing creator");
Assert(CreatorBatch.Apply(store,new MassEdit([1],Tags:[])) is null,"explicit empty replacement can clear tags");
Assert(store.Query("SELECT tags FROM creators WHERE id=1")[0]["tags"]!.ToString()=="[]","explicit tag replacement is persisted");
Assert(Convert.ToInt32(store.Query("SELECT monitor_video FROM creators WHERE id=1")[0]["monitor_video"])==0,"audio batch editing never opts creators into videos");
var registration=CreatorRegistration.Create(store,new NewCreator("New Creator",2,Aliases:["Alias"],Tags:["Sleep"],Identities:[new("soundgasm","Handle"),new("reddit","Handle")]));
Assert(registration.Id is not null&&registration.Error is null,"creator onboarding persists valid identities atomically");
Assert(store.Query("SELECT * FROM identities WHERE creator_id=$id",("id",registration.Id)).Count==2,"one creator can link multiple source identities");
var created=store.Query("SELECT * FROM creators WHERE id=$id",("id",registration.Id))[0];
Assert(Convert.ToInt32(created["monitor_video"])==0,"creator onboarding never opts into video");
Assert(!Directory.Exists(created["path"]!.ToString()),"creator onboarding does not write media directories");
var visual=CreatorRegistration.Create(store,new NewCreator("Visual Creator",2,MonitorVideo:true,VideoQualityProfileId:4));
var visualRow=store.Query("SELECT monitor_video,video_quality_profile_id FROM creators WHERE id=$id",("id",visual.Id))[0];
Assert(Convert.ToInt32(visualRow["monitor_video"])==1&&Convert.ToInt32(visualRow["video_quality_profile_id"])==4,"creator onboarding persists explicit video monitoring and profile selection");
Assert(CreatorRegistration.Create(store,new NewCreator("Invalid video profile",2,MonitorVideo:true,VideoQualityProfileId:999)).Error is not null,"creator onboarding validates video quality profile");
Assert(CreatorRegistration.Create(store,new NewCreator("new creator",2)).Error is not null,"case-insensitive duplicate creator is rejected");
Assert(CreatorRegistration.Create(store,new NewCreator("../Escape",2)).Error is not null,"creator path traversal is rejected");
Assert(CreatorRegistration.Create(store,new NewCreator("Invalid profile",999)).Error is not null,"onboarding validates acquisition profile");
Assert(CreatorRegistration.Create(store,new NewCreator("Conflicting Creator",2,Identities:[new("reddit","handle")])).Error is not null,"an identity cannot be owned by another creator");
Assert(store.Query("SELECT * FROM creators").Count==4,"rejected onboarding leaves no partial creators");
Assert(CreatorRegistration.Link(store,999,new("reddit","Unowned")).Error is not null,"linking validates creator ownership");
Assert(CreatorRegistration.Link(store,1,new("soundgasm","handle")).Error is not null,"linking cannot claim another creator identity");
Assert(CreatorRegistration.Link(store,1,new("soundgasm",null!)).Error is not null,"null source handles are rejected safely");
Assert(CreatorRegistration.Link(store,1,new("soundgasm","SecondHandle")).Id is not null,"existing creators can link additional source identities");
Assert(CreatorRegistration.Link(store,1,new("rss","HTTPS://Feeds.Example.test/Creator.xml?Series=Quiet")).Id is not null,"public RSS identities accept normalized provider URLs");
Assert(store.Query("SELECT handle FROM identities WHERE creator_id=1 AND kind='rss'")[0]["handle"]!.ToString()=="https://feeds.example.test/Creator.xml?Series=Quiet","public URL normalization preserves meaningful path and query case");
store.Execute("UPDATE creators SET monitor_video=1 WHERE id=1");
store.Execute("INSERT INTO backfill_jobs(id,creator_id,media_kind,state,cursor,started,updated) VALUES('resume-video',1,'Video','running','{\"phase\":\"known\",\"index\":2}',$now,$now)",("now",DateTimeOffset.UtcNow.ToString("O")));
store.InterruptRunningVideoBackfills();
Assert(store.Query("SELECT state FROM backfill_jobs WHERE id='resume-video'")[0]["state"]!.ToString()=="interrupted","service restart checkpoints an executing video history scan");
Assert(store.EnqueueResumableVideoBackfills(TimeSpan.FromMinutes(5))==1,"interrupted video history scan is requeued automatically");
Assert(store.Query("SELECT * FROM commands WHERE name='video-backfill' AND state='queued' AND CAST(json_extract(arguments,'$.creatorId') AS INTEGER)=1").Count==1,"resumed video history command retains creator scope");
Assert(store.EnqueueResumableVideoBackfills(TimeSpan.FromMinutes(5))==0,"recovery never duplicates an active video history command");
store.Execute("UPDATE commands SET state='failed' WHERE name='video-backfill';UPDATE backfill_jobs SET state='failed',updated=$old WHERE id='resume-video'",("old",DateTimeOffset.UtcNow.AddMinutes(-10).ToString("O")));
Assert(store.EnqueueResumableVideoBackfills(TimeSpan.FromMinutes(5))==1,"transient video history failures retry after the bounded delay");
store.Execute("UPDATE creators SET monitor_video=0 WHERE id=1;UPDATE commands SET state='failed' WHERE name='video-backfill';UPDATE backfill_jobs SET state='interrupted' WHERE id='resume-video'");
Assert(store.EnqueueResumableVideoBackfills(TimeSpan.Zero)==0,"disabled video monitoring never resumes a cancelled history scan");
store.Execute("INSERT INTO assets(key,url,targets,source,creator,title,state) VALUES('legacy-audio','fixture:legacy','[]','soundgasm','First','Legacy audio','complete'),('new-audio','fixture:new','[]','soundgasm','First','New audio','complete')");
store.Execute("UPDATE media_assets SET state='imported' WHERE recording_key='new-audio' AND media_kind='Audio'");
store.Execute("INSERT INTO media_assets(recording_key,media_kind,state) VALUES('legacy-audio','Video','imported')");
var totals=CreatorLibrary.Read(store).Single(row=>row["name"]!.ToString()=="First");
Assert(Convert.ToInt32(totals["audio_completed"])==2,"creator totals include legacy complete and new imported audio");
Assert(Convert.ToInt32(totals["video_completed"])==1,"video totals remain independent of audio compatibility states");
Assert(store.Query("SELECT state FROM media_assets WHERE recording_key='legacy-audio' AND media_kind='Audio'")[0]["state"]!.ToString()=="complete","creator totals preserve migrated audio state without rewriting records");
store.Execute("INSERT INTO media_assets(recording_key,media_kind,state,wanted,retry_after,error) VALUES('new-audio','Video','cancelled',0,999,'cancelled')");
Assert(store.ResumeCancelledVideos(1)==0,"disabled creator cannot reactivate cancelled videos");
store.Execute("UPDATE creators SET monitor_video=1 WHERE id=1");
Assert(store.ResumeCancelledVideos(2)==0,"video resume is scoped to its creator");
Assert(store.ResumeCancelledVideos(1)==1,"reenabling video monitoring restores previously cancelled work");
Assert(store.Query("SELECT * FROM media_assets WHERE recording_key='new-audio' AND media_kind='Video' AND state='wanted' AND wanted=1 AND retry_after=0 AND error IS NULL").Count==1,"resumed video is immediately eligible for its own queue");
Assert(store.Query("SELECT * FROM media_assets WHERE recording_key='legacy-audio' AND media_kind='Video' AND state='imported'").Count==1,"reenabling videos preserves imported video state");
Assert(store.Query("SELECT * FROM media_assets WHERE recording_key='new-audio' AND media_kind='Audio' AND state='imported'").Count==1,"reenabling videos preserves independent audio state");
store.Execute("UPDATE commands SET state='completed' WHERE name='video-backfill'");
Assert(store.EnqueueMonitoredVideoBackfills()==2,"periodic discovery scans full upload inventories only for opted-in creators");
Assert(store.EnqueueMonitoredVideoBackfills()==0,"periodic discovery does not duplicate queued full-history scans");
Assert(store.Query("SELECT * FROM commands WHERE name='video-backfill' AND state='queued' AND CAST(json_extract(arguments,'$.creatorId') AS INTEGER)=2").Count==0,"periodic video discovery respects other creators' opt-outs");
store.Execute("INSERT INTO creator_exclusions(name,aliases,identities,removed) VALUES('Excluded Reader','[\"Old Alias\"]','[{\"kind\":\"reddit\",\"handle\":\"OldHandle\"}]',1)");
Assert(CreatorRegistration.Create(store,new NewCreator("excluded reader",1)).Error is not null,"excluded creator cannot be recreated case-insensitively");
Assert(CreatorRegistration.Create(store,new NewCreator("Old Alias",1)).Error is not null,"removed creator aliases remain excluded");
Assert(CreatorRegistration.Create(store,new NewCreator("OldHandle",1)).Error is not null,"removed source handle cannot return as a differently named creator");
Assert(CreatorRegistration.Link(store,2,new("reddit","oldhandle")).Error is not null,"removed source identity cannot bypass exclusion through another creator");
store.Execute("INSERT INTO assets(key,url,targets,source,creator,title,state,saved_path) VALUES('retained','https://example.invalid/retained','[]','fixture','Retained Reader','Retained','complete','/fixture/retained.mp3')");
var readded=CreatorRegistration.Create(store,new NewCreator("retained reader",1));
Assert(readded.Id is not null,"kept recordings permit explicit creator re-addition");
Assert(store.Query("SELECT * FROM assets WHERE key='retained' AND creator='retained reader' AND saved_path='/fixture/retained.mp3' AND state='complete'").Count==1,"re-addition reconnects retained files without reacquiring or renaming");
Assert(CreatorRemoval.Request(store,2,new()).Error is not null,"removal cannot bypass production cutover");
store.Set("mode","production");
Assert(CreatorRemoval.Request(store,2,new()).Error is not null,"removal requires a preview token");
Assert(CreatorRemoval.Request(store,2,new(true,true,"wrong",new string('a',64))).Error is not null,"destructive removal requires exact creator name");
var removal=CreatorRemoval.Request(store,2,new(false,true,"",new string('a',64)));
Assert(removal.Id is not null,"keep-files removal becomes a durable provider command");
Assert(CreatorRemoval.Request(store,2,new(false,true,"",new string('a',64))).Error is not null,"duplicate concurrent removal is rejected");
Assert(store.Query("SELECT * FROM commands WHERE id=$id AND name='creator-remove' AND state='queued'",("id",removal.Id)).Count==1,"queued removal retains the reviewed options for serialized execution");
Console.WriteLine($"{assertions} store integration assertions passed");
// Credential-version validation also covers root-only resets: both replace the
// salted hash, so old HTTP cookies and open status connections lose access.
var authPath=Path.Combine(store.ConfigRoot,"auth.json");
var authFields=new Dictionary<string,string>{{"username","admin"},{"salt",Guid.NewGuid().ToString("N")},{"hash",Guid.NewGuid().ToString("N")},{"apiKey",Guid.NewGuid().ToString("N")}};
System.Text.Json.JsonElement AuthDocument()=>System.Text.Json.JsonSerializer.SerializeToElement(authFields);
File.WriteAllText(authPath,AuthDocument().GetRawText());
var guard=new SessionSecurity(store);
var session=new System.Security.Claims.ClaimsPrincipal(new System.Security.Claims.ClaimsIdentity(new[]{new System.Security.Claims.Claim(SessionSecurity.ClaimType,SessionSecurity.Stamp(AuthDocument()))},"Cookies"));
Assert(guard.IsCurrent(session),"current credential stamp accepts an authenticated session");
var oldCookie=new System.Security.Claims.ClaimsPrincipal(new System.Security.Claims.ClaimsIdentity(new[]{new System.Security.Claims.Claim("name","admin")},"Cookies"));
Assert(!guard.IsCurrent(oldCookie),"pre-hardening cookies without credential stamp are revoked");
var hub=new TestHubContext(session);guard.Track(hub);await guard.StartAsync(CancellationToken.None);
authFields["hash"]=Guid.NewGuid().ToString("N");
SessionSecurity.ReplaceCredentials(authPath,AuthDocument().GetRawText());
Assert(!guard.IsCurrent(session),"password replacement immediately rejects an old HTTP session");
Assert(OperatingSystem.IsWindows()||File.GetUnixFileMode(authPath)==(UnixFileMode.UserRead|UnixFileMode.UserWrite),"atomic password replacement keeps mode0600");
await Task.Delay(2500);
Assert(hub.Aborted,"password replacement closes existing SignalR status connections");
await guard.StopAsync(CancellationToken.None);guard.Dispose();
var apiPrincipal=new System.Security.Claims.ClaimsPrincipal(new System.Security.Claims.ClaimsIdentity(new[]{new System.Security.Claims.Claim("auth","api-key")},"api-key"));
Assert(guard.IsCurrent(apiPrincipal),"password reset does not implicitly rotate API authentication");
File.WriteAllText(authPath,"incomplete");
Assert(!guard.IsCurrent(session),"malformed authentication configuration fails closed");
File.WriteAllText(authPath,"{\"username\":[],\"salt\":{},\"hash\":false}");
Assert(!guard.IsCurrent(session),"wrong-type authentication fields fail closed without crashing status worker");
Console.WriteLine($"{assertions} total integration assertions passed");
Environment.SetEnvironmentVariable("ASMARR_STATE",Path.Combine(root,"fresh-state"));
Environment.SetEnvironmentVariable("ASMARR_CONFIG",Path.Combine(root,"fresh-config"));
Directory.CreateDirectory(Path.Combine(root,"fresh-config"));
File.WriteAllText(Path.Combine(root,"fresh-config","fresh-install.json"),System.Text.Json.JsonSerializer.Serialize(new {kind="fresh",audioRoot=Path.Combine(root,"audio"),videoRoot=Path.Combine(root,"video")}));
var fresh=new Store();fresh.Initialize();fresh.Initialize();
Assert(fresh.Setting("installation.kind")=="fresh"&&fresh.Setting("recovery.hold")=="true","fresh package initializes an inert setup hold without legacy migration");
Assert(fresh.Query("SELECT * FROM tasks WHERE enabled=1").Count==0,"fresh install starts no scheduled acquisition");
Assert(fresh.Query("SELECT * FROM migration_audits").Count==1,"fresh initialization remains idempotent and is not fabricated legacy acceptance");
Assert(fresh.Query("SELECT * FROM creators").Count==0,"fresh install does not invent creators");
Console.WriteLine($"{assertions} final integration assertions passed");
SqliteConnection.ClearAllPools();Directory.Delete(root,true);

sealed class TestHubContext(System.Security.Claims.ClaimsPrincipal user):Microsoft.AspNetCore.SignalR.HubCallerContext
{
    readonly CancellationTokenSource source=new();
    public bool Aborted{get;private set;}
    public override string ConnectionId=>"fixture-status-connection";
    public override string? UserIdentifier=>"fixture-admin";
    public override System.Security.Claims.ClaimsPrincipal? User=>user;
    public override IDictionary<object,object?> Items{get;}=new Dictionary<object,object?>();
    public override Microsoft.AspNetCore.Http.Features.IFeatureCollection Features{get;}=new Microsoft.AspNetCore.Http.Features.FeatureCollection();
    public override CancellationToken ConnectionAborted=>source.Token;
    public override void Abort(){Aborted=true;source.Cancel();}
}
