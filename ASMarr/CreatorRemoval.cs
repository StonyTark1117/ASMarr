using System.Text.Json;
using System.Text.RegularExpressions;

namespace ASMarr;

public record RemoveCreator(bool DeleteFiles=false,bool PreventReadd=true,string Confirmation="",string PlanToken="");
public record RemovalRequestResult(string? Id,string? Error);

public static class CreatorRemoval
{
    public static bool Excluded(Store store,string name,IEnumerable<LinkedIdentity> identities)
    {
        var supplied=identities.ToArray();
        foreach(var row in store.Query("SELECT * FROM creator_exclusions"))
        {
            var aliases=JsonSerializer.Deserialize<string[]>(row["aliases"]!.ToString()!)??[];
            if(aliases.Append(row["name"]!.ToString()!).Contains(name,StringComparer.OrdinalIgnoreCase))return true;
            using var linked=JsonDocument.Parse(row["identities"]!.ToString()!);
            if(linked.RootElement.EnumerateArray().Any(i=>string.Equals(name,i.GetProperty("handle").GetString(),StringComparison.OrdinalIgnoreCase)))return true;
            if(linked.RootElement.EnumerateArray().Any(i=>supplied.Any(s=>s.Kind==i.GetProperty("kind").GetString()&&string.Equals(s.Handle,i.GetProperty("handle").GetString(),StringComparison.OrdinalIgnoreCase))))return true;
        }
        return false;
    }
    public static RemovalRequestResult Request(Store store,int creatorId,RemoveCreator input)
    {
        if(store.Setting("mode")!="production")return new(null,"Removal is disabled until production cutover");
        using var db=store.Open();using var transaction=db.BeginTransaction();using var command=db.CreateCommand();command.Transaction=transaction;
        command.CommandText="SELECT name FROM creators WHERE id=$id";command.Parameters.AddWithValue("$id",creatorId);
        var name=command.ExecuteScalar()?.ToString();
        if(name is null)return new(null,"Creator no longer exists");
        if(!Regex.IsMatch(input.PlanToken??"","^[a-f0-9]{64}$"))return new(null,"Open a removal preview first");
        if(input.DeleteFiles&&input.Confirmation!=name)return new(null,"Type the exact creator name to delete files");
        command.CommandText="SELECT count(*) FROM commands WHERE name='creator-remove' AND state IN ('queued','running') AND json_extract(arguments,'$.creatorId')=$id";
        if(Convert.ToInt32(command.ExecuteScalar())>0)return new(null,"Removal is already queued for this creator");
        string id=Guid.NewGuid().ToString("N");
        command.Parameters.Clear();command.CommandText="INSERT INTO commands(id,name,arguments,state,created) VALUES($id,'creator-remove',$args,'queued',$now)";
        command.Parameters.AddWithValue("$id",id);command.Parameters.AddWithValue("$now",DateTimeOffset.UtcNow.ToString("O"));
        command.Parameters.AddWithValue("$args",JsonSerializer.Serialize(new {creatorId,deleteFiles=input.DeleteFiles,preventReadd=input.PreventReadd,confirmation=input.Confirmation,planToken=input.PlanToken}));
        command.ExecuteNonQuery();transaction.Commit();return new(id,null);
    }
}
