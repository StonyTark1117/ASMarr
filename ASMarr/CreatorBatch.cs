using System.Text.Json;

namespace ASMarr;

public record MassEdit(int[] Ids,bool? Monitored=null,int? ProfileId=null,string[]? Tags=null,string[]? AddTags=null);

public static class CreatorBatch
{
    public static string? Apply(Store store,MassEdit edit)
    {
        if(edit.Ids is null||edit.Ids.Length==0||edit.Ids.Length>1000||edit.Ids.Any(id=>id<=0))return "Select between 1 and 1000 valid creators";
        if(edit.Monitored is null&&edit.ProfileId is null&&edit.Tags is null&&edit.AddTags is null)return "Choose at least one edit";
        if((edit.Tags??[]).Concat(edit.AddTags??[]).Any(t=>string.IsNullOrWhiteSpace(t)||t.Length>100||t.Any(char.IsControl)))return "Tags must contain 1–100 visible characters";
        using var db=store.Open();using var transaction=db.BeginTransaction();
        using var command=db.CreateCommand();command.Transaction=transaction;
        if(edit.ProfileId is not null)
        {
            command.CommandText="SELECT count(*) FROM profiles WHERE id=$profile";command.Parameters.AddWithValue("$profile",edit.ProfileId.Value);
            if(Convert.ToInt32(command.ExecuteScalar())!=1)return "Acquisition profile does not exist";
        }
        foreach(var id in edit.Ids.Distinct())
        {
            command.Parameters.Clear();command.CommandText="SELECT tags FROM creators WHERE id=$id";command.Parameters.AddWithValue("$id",id);
            var existing=command.ExecuteScalar();if(existing is null)return "A selected creator no longer exists";
            string[]? tags=null;
            if(edit.Tags is not null||edit.AddTags is not null)
            {
                tags=(edit.Tags??JsonSerializer.Deserialize<string[]>(existing.ToString()!)??[])
                    .Concat(edit.AddTags??[]).Select(t=>t.Trim()).Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
                if(tags.Length>50)return "A creator may have at most 50 tags";
            }
            command.CommandText="UPDATE creators SET monitored=COALESCE($monitor,monitored),profile_id=COALESCE($profile,profile_id),tags=COALESCE($tags,tags) WHERE id=$id";
            command.Parameters.AddWithValue("$monitor",edit.Monitored is null?DBNull.Value:(object)(edit.Monitored.Value?1:0));
            command.Parameters.AddWithValue("$profile",edit.ProfileId is null?DBNull.Value:(object)edit.ProfileId.Value);
            command.Parameters.AddWithValue("$tags",tags is null?DBNull.Value:JsonSerializer.Serialize(tags));command.ExecuteNonQuery();
        }
        transaction.Commit();return null;
    }
}
