using System.Collections.Concurrent;
using System.Security.Claims;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.SignalR;

namespace ASMarr;

// A cookie is tied to the credentials that issued it, not just its expiry.
// No separate revocation database or password-reset coordination is necessary.
public sealed class SessionSecurity(Store store):BackgroundService
{
    public const string ClaimType="asmarr:credentials";
    readonly ConcurrentDictionary<string,HubCallerContext> connections=new();
    public static string Stamp(JsonElement auth)=>Convert.ToHexString(SHA256.HashData(
        Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new[]{auth.GetProperty("username").GetString(),
            auth.GetProperty("salt").GetString(),auth.GetProperty("hash").GetString()}))));
    public bool IsCurrent(ClaimsPrincipal? principal)
    {
        if(principal?.Identity?.IsAuthenticated!=true)return false;
        if(principal.FindFirst("auth")?.Value=="api-key")return true;
        try
        {
            using var auth=JsonDocument.Parse(File.ReadAllText(System.IO.Path.Combine(store.ConfigRoot,"auth.json")));
            var actual=principal.FindFirst(ClaimType)?.Value??"";
            return CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(actual),Encoding.UTF8.GetBytes(Stamp(auth.RootElement)));
        }
        catch(Exception e) when(e is IOException or UnauthorizedAccessException or JsonException or KeyNotFoundException or InvalidOperationException)
        {return false;} // Missing/incomplete configuration must never retain access.
    }
    public void Track(HubCallerContext context)=>connections[context.ConnectionId]=context;
    public void Untrack(string id)=>connections.TryRemove(id,out _);
    public static void ReplaceCredentials(string path,string json)
    {
        if(new FileInfo(path).LinkTarget is not null)throw new IOException("Linked credentials are not supported");
        var temporary=path+".new-"+Guid.NewGuid().ToString("N");
        try
        {
            var options=new FileStreamOptions{Mode=FileMode.CreateNew,Access=FileAccess.Write,Share=FileShare.None};
            if(!OperatingSystem.IsWindows())options.UnixCreateMode=UnixFileMode.UserRead|UnixFileMode.UserWrite;
            using(var stream=new FileStream(temporary,options))
            {
                var bytes=Encoding.UTF8.GetBytes(json);stream.Write(bytes);stream.Flush(true);
            }
            File.Move(temporary,path,true);
        }
        finally{if(File.Exists(temporary))File.Delete(temporary);}
    }
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        using var timer=new PeriodicTimer(TimeSpan.FromSeconds(2));
        while(await timer.WaitForNextTickAsync(stoppingToken))
            foreach(var pair in connections)
                if(!IsCurrent(pair.Value.User))
                {
                    pair.Value.Abort();
                    connections.TryRemove(pair.Key,out _);
                }
    }
}
