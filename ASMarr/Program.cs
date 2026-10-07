using ASMarr;
using System.Security.Claims;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authentication.Cookies;
using Microsoft.AspNetCore.DataProtection;
using Microsoft.AspNetCore.RateLimiting;
using System.Threading.RateLimiting;

var builder=WebApplication.CreateBuilder(args);
builder.Logging.ClearProviders();builder.Logging.AddJsonConsole();
var store=new Store();store.Initialize();
builder.Services.AddSingleton(store);
builder.Services.AddSingleton<ProviderProcess>();
builder.Services.AddSingleton<ISearchProvider,Prowlarr>();builder.Services.AddSingleton<IDownloadClient,QBittorrent>();builder.Services.AddSingleton<IMediaServer,Plex>();
builder.Services.AddSingleton<INotificationProvider,WebhookNotifications>();builder.Services.AddHttpClient();
builder.Services.AddSingleton<ISourceConnector>(sp=>new SourceConnector("reddit",sp.GetRequiredService<ProviderProcess>()));
builder.Services.AddSingleton<ISourceConnector>(sp=>new SourceConnector("soundgasm",sp.GetRequiredService<ProviderProcess>()));
builder.Services.AddSingleton<ISourceConnector>(sp=>new SourceConnector("youtube",sp.GetRequiredService<ProviderProcess>()));
builder.Services.AddSignalR();builder.Services.AddOpenApi();builder.Services.AddHostedService<Worker>();
builder.Services.AddDataProtection().PersistKeysToFileSystem(new DirectoryInfo(System.IO.Path.Combine(store.ConfigRoot,"keys"))).SetApplicationName("ASMarr");
builder.Services.AddAuthentication(CookieAuthenticationDefaults.AuthenticationScheme).AddCookie(o=> {
    o.Cookie.Name="__Host-ASMarr";o.Cookie.HttpOnly=true;o.Cookie.SecurePolicy=CookieSecurePolicy.Always;o.Cookie.SameSite=SameSiteMode.Strict;
    o.ExpireTimeSpan=TimeSpan.FromHours(12);o.SlidingExpiration=true;
    o.Events.OnRedirectToLogin=c=>{c.Response.StatusCode=401;return Task.CompletedTask;};
    o.Events.OnRedirectToAccessDenied=c=>{c.Response.StatusCode=403;return Task.CompletedTask;};
});
builder.Services.AddAuthorization();
builder.Services.AddRateLimiter(o=> {o.RejectionStatusCode=429;o.AddPolicy("login",context=>RateLimitPartition.GetFixedWindowLimiter(context.Connection.RemoteIpAddress?.ToString()??"unknown",_=>new FixedWindowRateLimiterOptions {PermitLimit=5,Window=TimeSpan.FromMinutes(1),QueueLimit=0}));});
var authPath=System.IO.Path.Combine(store.ConfigRoot,"auth.json");
if(!File.Exists(authPath))
{
    string password=Convert.ToHexString(RandomNumberGenerator.GetBytes(18)),apiKey=Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
    byte[] salt=RandomNumberGenerator.GetBytes(16);
    string hash=Convert.ToBase64String(Rfc2898DeriveBytes.Pbkdf2(password,salt,600000,HashAlgorithmName.SHA256,32));
    await File.WriteAllTextAsync(authPath,JsonSerializer.Serialize(new {username="admin",salt=Convert.ToBase64String(salt),hash,apiKey}));
    var bootstrap=System.IO.Path.Combine(store.ConfigRoot,"initial-admin.txt");await File.WriteAllTextAsync(bootstrap,"Username: admin\nPassword: "+password+"\n");
    if(!OperatingSystem.IsWindows()){File.SetUnixFileMode(authPath,UnixFileMode.UserRead|UnixFileMode.UserWrite);File.SetUnixFileMode(bootstrap,UnixFileMode.UserRead|UnixFileMode.UserWrite);}
}
JsonElement Auth()=>JsonDocument.Parse(File.ReadAllText(authPath)).RootElement.Clone();
bool Equal(string a,string b)=>CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(a),Encoding.UTF8.GetBytes(b));
var app=builder.Build();
app.Use(async(context,next)=> {
    context.Response.Headers["X-Content-Type-Options"]="nosniff";context.Response.Headers["X-Frame-Options"]="DENY";context.Response.Headers["Referrer-Policy"]="same-origin";
    context.Response.Headers["Content-Security-Policy"]="default-src 'self'; connect-src 'self' wss:; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'";
    await next();
});
app.UseRateLimiter();app.UseAuthentication();
app.Use(async(context,next)=> {
    if(context.Request.Headers.TryGetValue("X-Api-Key",out var key)&&Equal(key.ToString(),Auth().GetProperty("apiKey").GetString()!))
        context.User=new ClaimsPrincipal(new ClaimsIdentity(new[]{new Claim(ClaimTypes.Name,"admin"),new Claim("auth","api-key")},"api-key"));
    // Cookie mutations require a same-origin custom header. API keys cannot be
    // read by an unrelated origin; no cross-origin access is configured.
    if(context.Request.Path.StartsWithSegments("/api")&&context.Request.Method is not ("GET" or "HEAD" or "OPTIONS")&&context.User.Identity?.IsAuthenticated==true&&context.User.FindFirst("auth")?.Value!="api-key")
    {
        if(context.Request.Headers["X-ASMarr-Request"]!="1"){context.Response.StatusCode=403;return;}
        var origin=context.Request.Headers.Origin.ToString();
        if(origin.Length>0&&origin!=context.Request.Scheme+"://"+context.Request.Host){context.Response.StatusCode=403;return;}
    }
    await next();
});
app.UseAuthorization();app.UseDefaultFiles();app.UseStaticFiles();
app.MapGet("/healthz",()=>Results.Ok(new {application="ASMarr",status="ok"}));
app.MapPost("/api/v1/auth/login",async(HttpContext context,Login input)=> {
    if(context.Request.Headers["X-ASMarr-Request"]!="1")return Results.StatusCode(403);
    var auth=Auth();var salt=Convert.FromBase64String(auth.GetProperty("salt").GetString()!);
    var hash=Convert.ToBase64String(Rfc2898DeriveBytes.Pbkdf2(input.Password,salt,600000,HashAlgorithmName.SHA256,32));
    if(!Equal(input.Username,auth.GetProperty("username").GetString()!)||!Equal(hash,auth.GetProperty("hash").GetString()!))return Results.Unauthorized();
    await context.SignInAsync(new ClaimsPrincipal(new ClaimsIdentity(new[]{new Claim(ClaimTypes.Name,"admin")},CookieAuthenticationDefaults.AuthenticationScheme)));
    return Results.Ok(new {username="admin"});
}).RequireRateLimiting("login");
var api=app.MapGroup("/api/v1").RequireAuthorization();
api.MapGet("/auth",(HttpContext c)=>new {username=c.User.Identity!.Name});
api.MapPost("/auth/logout",async(HttpContext c)=>{await c.SignOutAsync();return Results.NoContent();});
api.MapPost("/auth/password",async(PasswordChange p)=> {
    if(p.Password.Length<12)return Results.BadRequest(new {error="Use at least 12 characters"});
    var a=Auth();var current=Convert.ToBase64String(Rfc2898DeriveBytes.Pbkdf2(p.CurrentPassword,Convert.FromBase64String(a.GetProperty("salt").GetString()!),600000,HashAlgorithmName.SHA256,32));
    if(!Equal(current,a.GetProperty("hash").GetString()!))return Results.Unauthorized();
    var salt=RandomNumberGenerator.GetBytes(16);var hash=Convert.ToBase64String(Rfc2898DeriveBytes.Pbkdf2(p.Password,salt,600000,HashAlgorithmName.SHA256,32));
    await File.WriteAllTextAsync(authPath,JsonSerializer.Serialize(new {username="admin",salt=Convert.ToBase64String(salt),hash,apiKey=a.GetProperty("apiKey").GetString()}));
    var initial=System.IO.Path.Combine(store.ConfigRoot,"initial-admin.txt");if(File.Exists(initial))File.Delete(initial);
    return Results.Ok(new {status="updated"});
});
api.MapGet("/system/status",()=>new {version="0.2.0",runtime=System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription,mode=store.Setting("mode"),root=store.Setting("root"),videoRoot=store.Setting("video.root"),counts=store.Query("SELECT state,count(*) AS count FROM assets GROUP BY state"),mediaCounts=store.Query("SELECT media_kind,state,count(*) AS count FROM media_assets GROUP BY media_kind,state"),creators=store.Query("SELECT count(*) AS count FROM creators"),sources=store.Query("SELECT count(*) AS count FROM sources"),shadowCycles=store.Query("SELECT id,started,finished,clean FROM shadow_cycles ORDER BY id DESC"),qualifiedShadowDays=store.Query("SELECT DISTINCT substr(started,1,10) AS day FROM shadow_cycles WHERE clean=1 AND json_extract(comparison,'$.legacyImplementationSha256') IS NOT NULL").Count,disk=Disk(store.Setting("root")),videoDisk=Disk(store.Setting("video.root")),scheduler="internal"});
api.MapGet("/creators",()=>store.Query("SELECT c.*,count(DISTINCT a.key) AS recordings,sum(CASE WHEN aa.state='imported' THEN 1 ELSE 0 END) AS audio_completed,sum(CASE WHEN va.state='imported' THEN 1 ELSE 0 END) AS video_completed FROM creators c LEFT JOIN assets a ON a.creator=c.name LEFT JOIN media_assets aa ON aa.recording_key=a.key AND aa.media_kind='Audio' LEFT JOIN media_assets va ON va.recording_key=a.key AND va.media_kind='Video' GROUP BY c.id ORDER BY c.name COLLATE NOCASE"));
api.MapGet("/creators/{id:int}",(int id)=>new {creator=store.Query("SELECT * FROM creators WHERE id=$id",("id",id)).FirstOrDefault(),identities=store.Query("SELECT * FROM identities WHERE creator_id=$id",("id",id)),recordings=store.Query("SELECT a.*,(SELECT state FROM media_assets WHERE recording_key=a.key AND media_kind='Audio') AS audio_state,(SELECT state FROM media_assets WHERE recording_key=a.key AND media_kind='Video') AS video_state FROM assets a WHERE creator=(SELECT name FROM creators WHERE id=$id) ORDER BY acquired DESC",("id",id)),backfill=store.Query("SELECT * FROM backfill_jobs WHERE creator_id=$id AND media_kind='Video' ORDER BY started DESC LIMIT 1",("id",id)).FirstOrDefault()});
api.MapGet("/creators/{id:int}/video-backfill-preview",(int id)=>new {knownCandidates=store.Query("SELECT count(DISTINCT a.key) AS count FROM assets a WHERE a.creator=(SELECT name FROM creators WHERE id=$id) AND (a.targets LIKE '%youtube%' OR a.targets LIKE '%v.redd.it%')",("id",id)).FirstOrDefault()?.GetValueOrDefault("count")??0,scope="complete known history"});
api.MapPut("/creators/{id:int}",(int id,CreatorEdit c)=> {
    var existing=store.Query("SELECT * FROM creators WHERE id=$id",("id",id)).FirstOrDefault();if(existing==null)return Results.NotFound();
    bool wasVideo=Convert.ToInt32(existing["monitor_video"])==1,monitorVideo=c.MonitorVideo??wasVideo;
    int videoProfile=c.VideoQualityProfileId??Convert.ToInt32(existing["video_quality_profile_id"]);
    if(store.Query("SELECT id FROM video_quality_profiles WHERE id=$id",("id",videoProfile)).Count==0)return Results.BadRequest(new{error="Unknown video quality profile"});
    store.Execute("UPDATE creators SET monitored=$monitored,profile_id=$profile,tags=$tags,monitor_video=$video,video_quality_profile_id=$videoProfile WHERE id=$id",("id",id),("monitored",c.Monitored?1:0),("profile",c.ProfileId),("tags",JsonSerializer.Serialize(c.Tags)),("video",monitorVideo?1:0),("videoProfile",videoProfile));
    string? commandId=null;
    if(!wasVideo&&monitorVideo)commandId=store.Enqueue("video-backfill",new{creatorId=id});
    if(wasVideo&&!monitorVideo){store.Execute("UPDATE media_assets SET wanted=0,state='cancelled' WHERE media_kind='Video' AND state IN ('wanted','queued','failed') AND recording_key IN (SELECT key FROM assets WHERE creator=(SELECT name FROM creators WHERE id=$id))",("id",id));store.Execute("UPDATE backfill_jobs SET state='cancelled',updated=$now,finished=$now WHERE creator_id=$id AND media_kind='Video' AND state IN ('queued','running','failed')",("id",id),("now",DateTimeOffset.UtcNow.ToString("O")));}
    return Results.Ok(new{monitorVideo,backfillCommandId=commandId});
});
api.MapPost("/creators/mass-edit",(MassEdit m)=> {var error=CreatorBatch.Apply(store,m);return error is null?Results.Ok(new{updated=m.Ids.Distinct().Count()}):Results.BadRequest(new{error});});
api.MapGet("/identities",()=>store.Query("SELECT i.*,c.name AS creator FROM identities i JOIN creators c ON c.id=i.creator_id ORDER BY c.name,i.kind"));
api.MapPost("/identities",(IdentityEdit i)=> {
    if(i.Kind is not ("reddit" or "soundgasm" or "youtube")||i.Handle.Length>100||!System.Text.RegularExpressions.Regex.IsMatch(i.Handle,@"^[\w-]+$"))return Results.BadRequest(new{error="Invalid source identity"});
    store.Execute("INSERT INTO identities(creator_id,kind,handle,enabled) VALUES($creator,$kind,$handle,$enabled)",("creator",i.CreatorId),("kind",i.Kind),("handle",i.Handle),("enabled",i.Enabled?1:0));return Results.Ok();
});
api.MapPut("/identities/{id:int}",(int id,JsonElement j)=>{store.Execute("UPDATE identities SET enabled=$enabled WHERE id=$id",("id",id),("enabled",j.GetProperty("enabled").GetBoolean()?1:0));return Results.Ok();});
api.MapGet("/recordings",(string? creator,string? state,string? q,int? limit,int? offset)=>store.Query("SELECT a.*,(SELECT state FROM media_assets WHERE recording_key=a.key AND media_kind='Audio') AS audio_state,(SELECT state FROM media_assets WHERE recording_key=a.key AND media_kind='Video') AS video_state FROM assets a WHERE ($creator IS NULL OR creator=$creator) AND ($state IS NULL OR state=$state OR EXISTS(SELECT 1 FROM media_assets m WHERE m.recording_key=a.key AND m.state=$state)) AND ($q IS NULL OR title LIKE '%'||$q||'%' OR creator LIKE '%'||$q||'%') ORDER BY COALESCE(published,acquired,0) DESC LIMIT $limit OFFSET $offset",("creator",creator),("state",state),("q",q),("limit",Math.Clamp(limit??200,1,1000)),("offset",Math.Max(0,offset??0))));
api.MapGet("/recordings/detail",(string key)=>new {recording=store.Query("SELECT * FROM assets WHERE key=$key",("key",key)).FirstOrDefault(),media=store.Query("SELECT * FROM media_assets WHERE recording_key=$key ORDER BY media_kind",("key",key)),candidates=store.Query("SELECT id,provider,provider_id,resolution,source_quality,bitrate,video_codec,audio_codec,container,interactive_only FROM video_candidates WHERE recording_key=$key ORDER BY resolution DESC",("key",key)),aliases=store.Query("SELECT * FROM aliases WHERE asset_key=$key",("key",key)),history=store.Query("SELECT * FROM history WHERE recording_key=$key ORDER BY id DESC",("key",key))});
api.MapPost("/recordings/action",(RecordingAction a)=> {
    string kind=a.MediaKind??"Audio";if(kind is not ("Audio" or "Video"))return Results.BadRequest();
    if(a.Action is "retry" or "suppress") { string state=a.Action=="retry"?"wanted":"suppressed";store.Execute("UPDATE media_assets SET state=$state,wanted=$wanted,retry_after=0,error=NULL WHERE recording_key=$key AND media_kind=$kind",("state",state),("wanted",a.Action=="retry"?1:0),("key",a.Key),("kind",kind));if(kind=="Audio")store.Execute("UPDATE assets SET state=$state,retry_after=0,error=NULL WHERE key=$key",("state",a.Action=="retry"?"pending":"suppressed"),("key",a.Key));return Results.Ok(); }
    if(a.Action=="blocklist") {store.Execute("INSERT INTO blocklist(recording_key,reason,created) VALUES($key,$reason,$at)",("key",a.Key),("reason",a.Reason??"Manual blocklist"),("at",DateTimeOffset.UtcNow.ToString("O")));store.Execute("UPDATE assets SET state='suppressed' WHERE key=$key",("key",a.Key));return Results.Ok();}
    return Results.BadRequest();
});
api.MapGet("/wanted",(string? mediaKind)=>store.Query("SELECT a.*,m.media_kind,m.state AS media_state,m.error AS media_error,m.retry_after AS media_retry_after FROM media_assets m JOIN assets a ON a.key=m.recording_key WHERE m.wanted=1 AND m.state IN ('wanted','failed','missing') AND ($kind IS NULL OR m.media_kind=$kind) ORDER BY m.retry_after,a.key",("kind",mediaKind)));
api.MapGet("/video/wanted",()=>store.Query("SELECT a.*,m.state AS media_state,m.error AS media_error FROM media_assets m JOIN assets a ON a.key=m.recording_key WHERE m.media_kind='Video' AND m.wanted=1 AND m.state IN ('wanted','failed','missing') ORDER BY m.retry_after,a.key"));
api.MapGet("/queue",(string? mediaKind)=>new {direct=store.Query("SELECT a.*,m.media_kind,m.state AS media_state FROM media_assets m JOIN assets a ON a.key=m.recording_key WHERE m.state IN ('wanted','queued','downloading','failed') AND ($kind IS NULL OR m.media_kind=$kind) ORDER BY m.retry_after",("kind",mediaKind)),downloads=store.Query("SELECT * FROM queue WHERE ($kind IS NULL OR media_kind=$kind) ORDER BY created DESC",("kind",mediaKind)),commands=store.Query("SELECT * FROM commands WHERE state IN ('queued','running') ORDER BY created DESC")});
api.MapGet("/video/queue",()=>new {direct=store.Query("SELECT a.*,m.state AS media_state FROM media_assets m JOIN assets a ON a.key=m.recording_key WHERE m.media_kind='Video' AND m.state IN ('wanted','queued','downloading','failed') ORDER BY m.retry_after"),downloads=store.Query("SELECT * FROM queue WHERE media_kind='Video' ORDER BY created DESC"),backfills=store.Query("SELECT * FROM backfill_jobs WHERE media_kind='Video' ORDER BY started DESC")});
api.MapGet("/history",(string? mediaKind)=>new {events=store.Query("SELECT * FROM history WHERE $kind IS NULL OR json_extract(details,'$.mediaKind')=$kind ORDER BY id DESC LIMIT 200",("kind",mediaKind)),runs=store.Query("SELECT * FROM runs ORDER BY started DESC LIMIT 100"),imports=store.Query("SELECT m.*,a.title,a.creator FROM media_assets m JOIN assets a ON a.key=m.recording_key WHERE m.state='imported' AND ($kind IS NULL OR m.media_kind=$kind) ORDER BY m.acquired DESC LIMIT 200",("kind",mediaKind))});
api.MapGet("/video/history",()=>new {assets=store.Query("SELECT a.*,m.* FROM media_assets m JOIN assets a ON a.key=m.recording_key WHERE m.media_kind='Video' AND m.state IN ('imported','failed','unavailable') ORDER BY m.acquired DESC"),backfills=store.Query("SELECT * FROM backfill_jobs WHERE media_kind='Video' ORDER BY started DESC")});
api.MapGet("/calendar",()=>store.Query("SELECT key,title,creator,published,state FROM assets WHERE published>0 ORDER BY published DESC"));
api.MapGet("/connectors",()=>new {sources=store.Query("SELECT * FROM sources ORDER BY name"),configuration=new[]{"reddit","soundgasm","youtube","sfw"}.Select(k=>new{kind=k,enabled=store.Setting("source."+k+".enabled","true")=="true"})});
api.MapPut("/connectors/{kind}",(string kind,JsonElement j)=>{if(kind is not ("reddit" or "soundgasm" or "youtube" or "sfw"))return Results.BadRequest();store.Set("source."+kind+".enabled",j.GetProperty("enabled").GetBoolean()?"true":"false");return Results.Ok();});
api.MapGet("/profiles",()=>store.Query("SELECT * FROM profiles"));
api.MapPut("/profiles/{id:int}",(int id,ProfileEdit p)=>{var error=ValidateProfile(p.Settings);if(error!=null)return Results.BadRequest(new{error});store.Execute("UPDATE profiles SET name=$name,settings=$settings WHERE id=$id",("id",id),("name",p.Name),("settings",p.Settings.GetRawText()));return Results.Ok();});
api.MapGet("/video/profiles",()=>store.Query("SELECT * FROM video_quality_profiles ORDER BY id"));
api.MapPut("/video/profiles/{id:int}",(int id,VideoProfileEdit p)=>{if(p.Resolution is not ("Any" or "2160p" or "1440p" or "1080p" or "720p" or "480p"))return Results.BadRequest(new{error="Unsupported resolution"});store.Execute("UPDATE video_quality_profiles SET name=$name,resolution=$resolution,settings=$settings WHERE id=$id",("id",id),("name",p.Name),("resolution",p.Resolution),("settings",p.Settings.GetRawText()));return Results.Ok();});
api.MapGet("/settings",()=>store.Query("SELECT * FROM settings WHERE key NOT LIKE '%secret%'"));
api.MapPut("/settings/{key}",(string key,JsonElement j)=> {if(key is not ("naming" or "root" or "video.root" or "video.free_space_gib" or "video.concurrency" or "video.naming"))return Results.BadRequest(new {error="Use the audited cutover procedure to change execution mode"});string value=j.GetProperty("value").GetString()??"";if((key is "root" or "video.root")&&!System.IO.Path.IsPathFullyQualified(value))return Results.BadRequest();if(key=="video.root"&&RootsOverlap(store.Setting("root"),value))return Results.BadRequest(new{error="Audio and video roots may not overlap"});if(key=="root"&&RootsOverlap(value,store.Setting("video.root")))return Results.BadRequest(new{error="Audio and video roots may not overlap"});if(key=="video.free_space_gib"&&(!double.TryParse(value,out var free)||free<0))return Results.BadRequest();if(key=="video.concurrency"&&(!int.TryParse(value,out var concurrency)||concurrency<1||concurrency>4))return Results.BadRequest();store.Set(key,value);return Results.Ok();});
api.MapGet("/integrations",()=>IntegrationStatus(store));
api.MapPut("/integrations/{kind}",async(string kind,JsonElement value)=> {
    if(kind is not ("prowlarr" or "qbittorrent" or "plex" or "notifications"))return Results.BadRequest();
    string path=System.IO.Path.Combine(store.ConfigRoot,"integrations.json");var data=File.Exists(path)?JsonSerializer.Deserialize<Dictionary<string,JsonElement>>(await File.ReadAllTextAsync(path))!:new();data[kind]=value;
    await File.WriteAllTextAsync(path,JsonSerializer.Serialize(data));if(!OperatingSystem.IsWindows())File.SetUnixFileMode(path,UnixFileMode.UserRead|UnixFileMode.UserWrite);
    return Results.Ok();
});
foreach(string resource in new[]{"searches","download-clients","media-servers"})api.MapGet("/"+resource,()=>IntegrationStatus(store));
api.MapGet("/tasks",()=>store.Query("SELECT * FROM tasks ORDER BY name"));
api.MapPut("/tasks/{name}",(string name,TaskEdit t)=>{store.Execute("UPDATE tasks SET enabled=$enabled,interval_seconds=$seconds WHERE name=$name",("name",name),("enabled",t.Enabled?1:0),("seconds",Math.Clamp(t.IntervalSeconds,60,604800)));return Results.Ok();});
api.MapGet("/commands",()=>store.Query("SELECT * FROM commands ORDER BY created DESC LIMIT 100"));
api.MapGet("/commands/{id}",(string id)=>store.Query("SELECT * FROM commands WHERE id=$id",("id",id)).FirstOrDefault());
api.MapPost("/commands",(CommandInput c)=> {
    if(!new[]{"migration","discovery","queue","video-queue","video-backfill","disk-scan","plex","plex-verify","plex-video","plex-video-verify","playlists","playlists-verify","health","backup","source-test","search","grab","manual-import","rename-preview","integration-test"}.Contains(c.Name))return Results.BadRequest(new{error="Unknown command"});
    if(store.Setting("mode")!="production"&&new[]{"grab","manual-import","video-queue","plex-video"}.Contains(c.Name))return Results.Conflict(new{error="Acquisition is disabled until audited cutover"});
    return Results.Accepted(value:new {id=store.Enqueue(c.Name,c.Arguments)});
});
api.MapGet("/health",()=>new {database=store.Query("PRAGMA quick_check"),sources=store.Query("SELECT name,status,last_success FROM sources"),failedCommands=store.Query("SELECT id,name,error FROM commands WHERE state='failed' ORDER BY created DESC LIMIT 10"),migration=store.Query("SELECT * FROM migration_audits ORDER BY id DESC LIMIT 1")});
api.MapGet("/logs",()=>store.Query("SELECT * FROM logs ORDER BY id DESC LIMIT 500"));
api.MapGet("/backups",()=>Directory.EnumerateFiles(System.IO.Path.Combine(store.Root,"backups"),"*.db").Select(p=>new {name=System.IO.Path.GetFileName(p),size=new FileInfo(p).Length,created=new FileInfo(p).CreationTimeUtc}));
api.MapGet("/shadow-cycles",()=>store.Query("SELECT * FROM shadow_cycles ORDER BY id DESC"));
api.MapGet("/system/updates",()=>new {version="0.2.0",automatic=false,channel="local",message="Updates are installed through an audited deployment"});
app.MapHub<StatusHub>("/api/v1/events").RequireAuthorization();app.MapOpenApi().RequireAuthorization();
app.MapFallbackToFile("index.html");app.Run();

static object Disk(string path) {try{var drive=DriveInfo.GetDrives().Where(d=>path.StartsWith(d.Name,StringComparison.Ordinal)).OrderByDescending(d=>d.Name.Length).First();return new{available=drive.AvailableFreeSpace,total=drive.TotalSize};}catch{return new{available=0L,total=0L};}}
static bool RootsOverlap(string first,string second){if(!System.IO.Path.IsPathFullyQualified(first)||!System.IO.Path.IsPathFullyQualified(second))return false;var a=System.IO.Path.GetFullPath(first).TrimEnd(System.IO.Path.DirectorySeparatorChar)+System.IO.Path.DirectorySeparatorChar;var b=System.IO.Path.GetFullPath(second).TrimEnd(System.IO.Path.DirectorySeparatorChar)+System.IO.Path.DirectorySeparatorChar;return a.StartsWith(b,StringComparison.Ordinal)||b.StartsWith(a,StringComparison.Ordinal);}
static string? ValidateProfile(JsonElement settings)
{
    if(settings.ValueKind!=JsonValueKind.Object)return "Profile settings must be an object";
    var codes=new HashSet<string>(new[]{"F","M","NB","A","ANY"},StringComparer.OrdinalIgnoreCase);
    foreach(var key in new[]{"allowedSpeakers","allowedAudiences"})if(settings.TryGetProperty(key,out var values))
    {
        if(values.ValueKind!=JsonValueKind.Array||values.GetArrayLength()==0||values.EnumerateArray().Any(x=>x.ValueKind!=JsonValueKind.String||!codes.Contains(x.GetString()!)))return "Invalid "+key;
    }
    foreach(var key in new[]{"requiredTopics","excludedTerms"})if(settings.TryGetProperty(key,out var values))
    {
        if(values.ValueKind!=JsonValueKind.Array||values.EnumerateArray().Any(x=>x.ValueKind!=JsonValueKind.String||string.IsNullOrWhiteSpace(x.GetString())||x.GetString()!.Length>200))return "Invalid "+key;
    }
    foreach(var key in new[]{"requireSpeakerTag","trustedMissingSpeakerTag"})if(settings.TryGetProperty(key,out var value)&&value.ValueKind is not (JsonValueKind.True or JsonValueKind.False))return "Invalid "+key;
    if(settings.TryGetProperty("topicMatch",out var match)&&(match.ValueKind!=JsonValueKind.String||match.GetString() is not ("any" or "all")))return "Invalid topicMatch";
    foreach(var key in new[]{"minimumDuration","backlogLimit"})if(settings.TryGetProperty(key,out var value)&&(value.ValueKind!=JsonValueKind.Number||!value.TryGetDouble(out var number)||double.IsNaN(number)||double.IsInfinity(number)||number<0))return "Invalid "+key;
    if(settings.TryGetProperty("backlogLimit",out var backlog)&&(!backlog.TryGetInt32(out _)))return "Invalid backlogLimit";
    return null;
}
static object IntegrationStatus(Store s) {var p=System.IO.Path.Combine(s.ConfigRoot,"integrations.json");if(!File.Exists(p))return new Dictionary<string,object>();using var d=JsonDocument.Parse(File.ReadAllText(p));return d.RootElement.EnumerateObject().ToDictionary(x=>x.Name,x=>(object)new{configured=true,url=x.Value.TryGetProperty("url",out var u)?u.GetString():null});}
record Login(string Username,string Password);
record PasswordChange(string CurrentPassword,string Password);
record CreatorEdit(bool Monitored,int ProfileId,string[] Tags,bool? MonitorVideo,int? VideoQualityProfileId);
record IdentityEdit(int CreatorId,string Kind,string Handle,bool Enabled);
record ProfileEdit(string Name,JsonElement Settings);
record VideoProfileEdit(string Name,string Resolution,JsonElement Settings);
record RecordingAction(string Key,string Action,string? Reason,string? MediaKind);
record CommandInput(string Name,JsonElement Arguments);
record TaskEdit(bool Enabled,int IntervalSeconds);
