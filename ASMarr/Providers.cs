using System.Diagnostics;
using System.Text.Json;

namespace ASMarr;

public interface ISourceConnector
{
    string Kind { get; }
    Task<JsonElement> ValidateCredentials(CancellationToken ct);
    Task<JsonElement> EnumerateRecordings(bool shadow,CancellationToken ct);
    Task<JsonElement> ResolveMetadata(string key,CancellationToken ct);
    Task<JsonElement> AcquireDirectMedia(string key,CancellationToken ct);
}
public interface ISearchProvider { Task<JsonElement> Search(string key,CancellationToken ct); }
public interface IDownloadClient
{
    Task<JsonElement> Submit(string key,JsonElement candidate,CancellationToken ct);
    Task<JsonElement> Monitor(CancellationToken ct);
    Task<JsonElement> Remove(string downloadId,CancellationToken ct);
}
public interface IMediaServer
{
    Task<JsonElement> RefreshPaths(CancellationToken ct);
    Task<JsonElement> VerifyIndexing(CancellationToken ct);
    Task<JsonElement> UpdateMetadata(CancellationToken ct);
    Task<JsonElement> MaintainPlaylists(bool preview,CancellationToken ct);
}
public interface INotificationProvider { Task Notify(string message,CancellationToken ct); }

public sealed class ProviderProcess(Store store)
{
    public async Task<JsonElement> Run(string operation,object arguments,CancellationToken ct)
    {
        var info=new ProcessStartInfo("/usr/bin/python3") { RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true,UseShellExecute=false };
        info.ArgumentList.Add(System.IO.Path.Combine(AppContext.BaseDirectory,"providers","bridge.py"));
        info.Environment["ASMARR_STATE"]=store.Root; info.Environment["ASMARR_CONFIG"]=store.ConfigRoot;
        using var p=Process.Start(info) ?? throw new InvalidOperationException("Provider could not start");
        using var registration=ct.Register(()=> { try { p.Kill(true); } catch(InvalidOperationException) {} });
        await p.StandardInput.WriteAsync(JsonSerializer.Serialize(new { operation,arguments })); p.StandardInput.Close();
        var stdout=p.StandardOutput.ReadToEndAsync(ct); var stderr=p.StandardError.ReadToEndAsync(ct);
        await p.WaitForExitAsync(ct); string output=await stdout; await stderr;
        if(p.ExitCode!=0) throw new InvalidOperationException($"Provider {operation} failed (exit {p.ExitCode}); inspect protected provider diagnostics");
        return JsonDocument.Parse(output).RootElement.Clone();
    }
}
public sealed class SourceConnector(string kind,ProviderProcess process):ISourceConnector
{
    public string Kind => kind;
    public Task<JsonElement> ValidateCredentials(CancellationToken ct)=>process.Run("test-source",new {kind},ct);
    public Task<JsonElement> EnumerateRecordings(bool shadow,CancellationToken ct)=>process.Run("discover",new {kind,shadow},ct);
    public Task<JsonElement> ResolveMetadata(string key,CancellationToken ct)=>process.Run("metadata",new {key},ct);
    public Task<JsonElement> AcquireDirectMedia(string key,CancellationToken ct)=>process.Run("acquire",new {key},ct);
}
public sealed class Prowlarr(ProviderProcess p):ISearchProvider { public Task<JsonElement> Search(string key,CancellationToken ct)=>p.Run("search",new {key},ct); }
public sealed class QBittorrent(ProviderProcess p):IDownloadClient
{
    public Task<JsonElement> Submit(string key,JsonElement candidate,CancellationToken ct)=>p.Run("grab",new {key,candidate},ct);
    public Task<JsonElement> Monitor(CancellationToken ct)=>p.Run("downloads",new {},ct);
    public Task<JsonElement> Remove(string downloadId,CancellationToken ct)=>p.Run("remove-download",new {downloadId},ct);
}
public sealed class Plex(ProviderProcess p):IMediaServer
{
    public Task<JsonElement> RefreshPaths(CancellationToken ct)=>p.Run("plex",new {},ct);
    public Task<JsonElement> VerifyIndexing(CancellationToken ct)=>p.Run("plex-verify",new {},ct);
    public Task<JsonElement> UpdateMetadata(CancellationToken ct)=>p.Run("playlists",new {preview=false},ct);
    public Task<JsonElement> MaintainPlaylists(bool preview,CancellationToken ct)=>p.Run("playlists",new {preview},ct);
}
public sealed class WebhookNotifications(Store store,IHttpClientFactory factory):INotificationProvider
{
    public async Task Notify(string message,CancellationToken ct)
    {
        var path=System.IO.Path.Combine(store.ConfigRoot,"integrations.json"); if(!File.Exists(path))return;
        using var doc=JsonDocument.Parse(await File.ReadAllTextAsync(path,ct));
        if(!doc.RootElement.TryGetProperty("notifications",out var config))return;
        foreach(var type in new[]{"webhook","discord"}) if(config.TryGetProperty(type,out var url)&&Uri.TryCreate(url.GetString(),UriKind.Absolute,out var uri))
        {
            using var response=await factory.CreateClient().PostAsJsonAsync(uri,type=="discord" ? (object)new{content=message}:new{application="ASMarr",message},ct);
            response.EnsureSuccessStatusCode();
        }
    }
}
