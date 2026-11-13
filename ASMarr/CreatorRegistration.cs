using System.Text.Json;
using System.Text.RegularExpressions;
using System.Text;

namespace ASMarr;

public record LinkedIdentity(string Kind,string Handle,bool Enabled=true);
public record NewCreator(string Name,int ProfileId,string[]? Aliases=null,string[]? Tags=null,bool Monitored=true,LinkedIdentity[]? Identities=null,bool MonitorVideo=false,int VideoQualityProfileId=1);
public record RegistrationResult(long? Id,string? Error);

public static class CreatorRegistration
{
    static readonly string[] PublicKinds=["rss","internet_archive","bandcamp"];
    public static string? NormalizeIdentity(string kind,string? handle)
    {
        handle=handle?.Trim();if(string.IsNullOrEmpty(handle))return null;
        if(kind=="internet_archive")
        {
            if(Uri.TryCreate(handle,UriKind.Absolute,out var archive))
            {
                if(archive.Scheme is not ("http" or "https")||archive.Host is not ("archive.org" or "www.archive.org"))return null;
                var parts=archive.AbsolutePath.Split('/',StringSplitOptions.RemoveEmptyEntries);
                if(parts.Length<2||parts[0] is not ("details" or "metadata"))return null;
                handle=Uri.UnescapeDataString(parts[1]);
                var query=System.Web.HttpUtility.ParseQueryString(archive.Query);var selector=query["creator"]?.Trim();
                if(!string.IsNullOrEmpty(selector))handle="collection:"+handle+"?creator="+Uri.EscapeDataString(selector);
            }
            if(handle.StartsWith("collection:",StringComparison.Ordinal))
            {
                var match=Regex.Match(handle,"^collection:([A-Za-z0-9_.-]{1,160})\\?creator=([^&#]+)$");
                if(!match.Success)return null;var selector=Uri.UnescapeDataString(match.Groups[2].Value).Trim();
                return selector.Length==0?null:"collection:"+match.Groups[1].Value+"?creator="+Uri.EscapeDataString(selector);
            }
            return Regex.IsMatch(handle,"^[A-Za-z0-9_.-]{1,160}$")?handle:null;
        }
        if(kind is "rss" or "bandcamp")
        {
            if(!Uri.TryCreate(handle,UriKind.Absolute,out var uri)||uri.Scheme is not ("http" or "https")||!string.IsNullOrEmpty(uri.UserInfo)||uri.Port is not (-1 or 80 or 443))return null;
            var host=uri.IdnHost.ToLowerInvariant();
            if(kind=="bandcamp"&&(host=="bandcamp.com"||!host.EndsWith(".bandcamp.com",StringComparison.Ordinal)))return null;
            var path=uri.AbsolutePath.TrimEnd('/');if(path=="/music")path="";
            if(kind=="bandcamp"&&path.Length>0&&!Regex.IsMatch(path,"^/(album|track)/[^/?#]+$"))return null;
            var builder=new UriBuilder(uri.Scheme.ToLowerInvariant(),host,uri.IsDefaultPort?-1:uri.Port,path.Length==0?"/":path);
            builder.Fragment="";builder.Query=kind=="rss"?uri.Query.TrimStart('?'):"";
            return builder.Uri.AbsoluteUri.TrimEnd('/')+(path.Length==0?"/":"");
        }
        if(kind is not ("reddit" or "soundgasm" or "youtube" or "whyp" or "audiochan" or "hotaudio"))return null;
        return Regex.IsMatch(handle,kind=="whyp"?"^@?[A-Za-z0-9_-]{1,100}$":"^[A-Za-z0-9_-]{1,100}$")?handle:null;
    }
    static bool ValidIdentity(LinkedIdentity? identity)=>identity is not null&&NormalizeIdentity(identity.Kind,identity.Handle) is not null;
    public static RegistrationResult Link(Store store,int creatorId,LinkedIdentity identity)
    {
        if(!ValidIdentity(identity))return new(null,"Invalid source identity");
        identity=identity with {Handle=NormalizeIdentity(identity.Kind,identity.Handle)!};
        using var db=store.Open();using var transaction=db.BeginTransaction();using var command=db.CreateCommand();command.Transaction=transaction;
        if(CreatorRemoval.Excluded(store,"",[identity]))return new(null,"Identity is excluded. Allow re-addition in Settings first");
        command.CommandText="SELECT count(*) FROM creators WHERE id=$id";command.Parameters.AddWithValue("$id",creatorId);
        if(Convert.ToInt32(command.ExecuteScalar())!=1)return new(null,"Creator does not exist");
        command.Parameters.Clear();command.CommandText="SELECT count(*) FROM identities WHERE kind=$kind AND handle=$handle COLLATE NOCASE";
        command.Parameters.AddWithValue("$kind",identity.Kind);command.Parameters.AddWithValue("$handle",identity.Handle);
        if(Convert.ToInt32(command.ExecuteScalar())!=0)return new(null,"Source identity is already linked");
        command.CommandText="INSERT INTO identities(creator_id,kind,handle,enabled) VALUES($creator,$kind,$handle,$enabled) RETURNING id";
        command.Parameters.AddWithValue("$creator",creatorId);command.Parameters.AddWithValue("$enabled",identity.Enabled?1:0);
        var id=Convert.ToInt64(command.ExecuteScalar());
        command.Parameters.Clear();command.CommandText="UPDATE creators SET follow_mode='explicit',follow_mode_review=0 WHERE id=$creator";
        command.Parameters.AddWithValue("$creator",creatorId);command.ExecuteNonQuery();
        transaction.Commit();return new(id,null);
    }
    public static RegistrationResult Create(Store store,NewCreator input)
    {
        string name=Regex.Replace(input.Name?.Trim()??"",@"\s+"," ");
        if(name.Length is <1 or >100||Encoding.UTF8.GetByteCount(name)>100||name.Any(char.IsControl)||name.IndexOfAny("/\\:*?\"<>|".ToCharArray())>=0||name.StartsWith('.')||name.EndsWith('.'))return new(null,"Use a visible creator name without path separators or reserved filename characters (maximum 100 UTF-8 bytes)");
        var aliases=input.Aliases??[];var tags=input.Tags??[];var identities=(input.Identities??[]).Select(i=>i with {Handle=NormalizeIdentity(i.Kind,i.Handle)??i.Handle}).ToArray();
        if(aliases.Length>50||tags.Length>50||aliases.Concat(tags).Any(x=>string.IsNullOrWhiteSpace(x)||x.Length>100||x.Any(char.IsControl)))return new(null,"Use at most 50 visible aliases and tags, each up to 100 characters");
        if(identities.Length>100||identities.Any(i=>!ValidIdentity(i)))return new(null,"Invalid source identity");
        string root=store.Setting("root");
        if(!Path.IsPathFullyQualified(root))return new(null,"Configure an absolute library root first");
        using var db=store.Open();using var transaction=db.BeginTransaction();using var command=db.CreateCommand();command.Transaction=transaction;
        if(CreatorRemoval.Excluded(store,name,identities)||aliases.Any(alias=>CreatorRemoval.Excluded(store,alias,[])))return new(null,"Creator is excluded. Allow re-addition in Settings first");
        command.CommandText="SELECT count(*) FROM profiles WHERE id=$id";command.Parameters.AddWithValue("$id",input.ProfileId);
        if(Convert.ToInt32(command.ExecuteScalar())!=1)return new(null,"Acquisition profile does not exist");
        command.Parameters.Clear();command.CommandText="SELECT count(*) FROM video_quality_profiles WHERE id=$id";command.Parameters.AddWithValue("$id",input.VideoQualityProfileId);
        if(Convert.ToInt32(command.ExecuteScalar())!=1)return new(null,"Video quality profile does not exist");
        command.Parameters.Clear();command.CommandText="SELECT count(*) FROM creators WHERE name=$name COLLATE NOCASE";command.Parameters.AddWithValue("$name",name);
        if(Convert.ToInt32(command.ExecuteScalar())!=0)return new(null,"Creator already exists");
        foreach(var identity in identities)
        {
            command.Parameters.Clear();command.CommandText="SELECT count(*) FROM identities WHERE kind=$kind AND handle=$handle COLLATE NOCASE";
            command.Parameters.AddWithValue("$kind",identity.Kind);command.Parameters.AddWithValue("$handle",identity.Handle);
            if(Convert.ToInt32(command.ExecuteScalar())!=0)return new(null,"Source identity is already linked to another creator");
        }
        command.Parameters.Clear();command.CommandText="INSERT INTO creators(name,path,profile_id,aliases,tags,monitored,monitor_video,video_quality_profile_id) VALUES($name,$path,$profile,$aliases,$tags,$monitor,$monitorVideo,$videoProfile) RETURNING id";
        command.Parameters.AddWithValue("$name",name);command.Parameters.AddWithValue("$path",Path.Combine(root,name));command.Parameters.AddWithValue("$profile",input.ProfileId);
        command.Parameters.AddWithValue("$aliases",JsonSerializer.Serialize(aliases.Select(x=>x.Trim()).Distinct(StringComparer.OrdinalIgnoreCase)));
        command.Parameters.AddWithValue("$tags",JsonSerializer.Serialize(tags.Select(x=>x.Trim()).Distinct(StringComparer.OrdinalIgnoreCase)));command.Parameters.AddWithValue("$monitor",input.Monitored?1:0);
        command.Parameters.AddWithValue("$monitorVideo",input.MonitorVideo?1:0);command.Parameters.AddWithValue("$videoProfile",input.VideoQualityProfileId);
        long id=Convert.ToInt64(command.ExecuteScalar());
        foreach(var identity in identities.DistinctBy(i=>(i.Kind,i.Handle.ToLowerInvariant())))
        {
            command.Parameters.Clear();command.CommandText="INSERT INTO identities(creator_id,kind,handle,enabled) VALUES($creator,$kind,$handle,$enabled)";
            command.Parameters.AddWithValue("$creator",id);command.Parameters.AddWithValue("$kind",identity.Kind);command.Parameters.AddWithValue("$handle",identity.Handle);command.Parameters.AddWithValue("$enabled",identity.Enabled?1:0);command.ExecuteNonQuery();
        }
        command.Parameters.Clear();command.CommandText="UPDATE assets SET creator=$name WHERE creator=$name COLLATE NOCASE";
        command.Parameters.AddWithValue("$name",name);command.ExecuteNonQuery();
        transaction.Commit();return new(id,null);
    }
}
