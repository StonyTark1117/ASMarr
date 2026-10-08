using System.Text.Json;
using System.Text.RegularExpressions;

namespace ASMarr;

public static class ShadowQualification
{
    static readonly string[] ParityFields = [
        "discoveryParity", "eligibilityAndSourceParity", "checkpointCalculationParity",
        "productionCheckpointsUnchanged", "applicationCheckpointsUnchanged", "mediaUnchanged",
        "playlistPreviewHealthy", "playlistCalculationParity"
    ];

    public static int Count(Store store)
    {
        try
        {
            var manifestPath=Path.Combine(store.Root,"deployed-release-runtime.json");
            using var manifest=JsonDocument.Parse(File.ReadAllText(manifestPath));
            var root=manifest.RootElement;
            var source=root.GetProperty("sourceCommit").GetString();
            var artifact=root.GetProperty("artifactSha256").GetString();
            var deployed=DateTimeOffset.Parse(root.GetProperty("deployedAt").GetString()!);
            if(!Regex.IsMatch(source??"", "^[a-f0-9]{40}$")||!Regex.IsMatch(artifact??"", "^[a-f0-9]{64}$"))return 0;
            var days=new HashSet<string>();
            foreach(var row in store.Query("SELECT started,comparison FROM shadow_cycles WHERE clean=1 ORDER BY started"))
            {
                if(!DateTimeOffset.TryParse(row["started"]?.ToString(),out var started)||started<deployed)continue;
                try
                {
                    using var comparison=JsonDocument.Parse(row["comparison"]?.ToString()??"{}");
                    var value=comparison.RootElement;
                    if(value.GetProperty("sourceCommit").GetString()!=source||value.GetProperty("artifactSha256").GetString()!=artifact)continue;
                    if(!value.TryGetProperty("legacyImplementationSha256",out var legacy)||string.IsNullOrWhiteSpace(legacy.GetString()))continue;
                    if(ParityFields.Any(field=>!value.TryGetProperty(field,out var parity)||parity.ValueKind!=JsonValueKind.True))continue;
                    days.Add(started.UtcDateTime.ToString("yyyy-MM-dd"));
                }
                catch(JsonException) { }
                catch(InvalidOperationException) { }
                catch(KeyNotFoundException) { }
            }
            return days.Count;
        }
        catch(IOException) { return 0; }
        catch(UnauthorizedAccessException) { return 0; }
        catch(JsonException) { return 0; }
        catch(InvalidOperationException) { return 0; }
        catch(KeyNotFoundException) { return 0; }
        catch(FormatException) { return 0; }
    }
}
