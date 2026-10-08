using System.Text.Json;
using System.Text.RegularExpressions;
using System.Text;

namespace ASMarr;

public record LinkedIdentity(string Kind,string Handle,bool Enabled=true);
public record NewCreator(string Name,int ProfileId,string[]? Aliases=null,string[]? Tags=null,bool Monitored=true,LinkedIdentity[]? Identities=null,bool MonitorVideo=false,int VideoQualityProfileId=1);
public record RegistrationResult(long? Id,string? Error);

public static class CreatorRegistration
{
    static bool ValidIdentity(LinkedIdentity? identity)=>identity is not null&&identity.Kind is "reddit" or "soundgasm" or "youtube"&&identity.Handle is not null&&Regex.IsMatch(identity.Handle,"^[A-Za-z0-9_-]{1,100}$");
    public static RegistrationResult Link(Store store,int creatorId,LinkedIdentity identity)
    {
        if(!ValidIdentity(identity))return new(null,"Invalid source identity");
        using var db=store.Open();using var transaction=db.BeginTransaction();using var command=db.CreateCommand();command.Transaction=transaction;
        command.CommandText="SELECT count(*) FROM creators WHERE id=$id";command.Parameters.AddWithValue("$id",creatorId);
        if(Convert.ToInt32(command.ExecuteScalar())!=1)return new(null,"Creator does not exist");
        command.Parameters.Clear();command.CommandText="SELECT count(*) FROM identities WHERE kind=$kind AND handle=$handle COLLATE NOCASE";
        command.Parameters.AddWithValue("$kind",identity.Kind);command.Parameters.AddWithValue("$handle",identity.Handle);
        if(Convert.ToInt32(command.ExecuteScalar())!=0)return new(null,"Source identity is already linked");
        command.CommandText="INSERT INTO identities(creator_id,kind,handle,enabled) VALUES($creator,$kind,$handle,$enabled) RETURNING id";
        command.Parameters.AddWithValue("$creator",creatorId);command.Parameters.AddWithValue("$enabled",identity.Enabled?1:0);
        var id=Convert.ToInt64(command.ExecuteScalar());transaction.Commit();return new(id,null);
    }
    public static RegistrationResult Create(Store store,NewCreator input)
    {
        string name=Regex.Replace(input.Name?.Trim()??"",@"\s+"," ");
        if(name.Length is <1 or >100||Encoding.UTF8.GetByteCount(name)>100||name.Any(char.IsControl)||name.IndexOfAny("/\\:*?\"<>|".ToCharArray())>=0||name.StartsWith('.')||name.EndsWith('.'))return new(null,"Use a visible creator name without path separators or reserved filename characters (maximum 100 UTF-8 bytes)");
        var aliases=input.Aliases??[];var tags=input.Tags??[];var identities=input.Identities??[];
        if(aliases.Length>50||tags.Length>50||aliases.Concat(tags).Any(x=>string.IsNullOrWhiteSpace(x)||x.Length>100||x.Any(char.IsControl)))return new(null,"Use at most 50 visible aliases and tags, each up to 100 characters");
        if(identities.Length>100||identities.Any(i=>!ValidIdentity(i)))return new(null,"Invalid source identity");
        string root=store.Setting("root");
        if(!Path.IsPathFullyQualified(root))return new(null,"Configure an absolute library root first");
        using var db=store.Open();using var transaction=db.BeginTransaction();using var command=db.CreateCommand();command.Transaction=transaction;
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
        transaction.Commit();return new(id,null);
    }
}
