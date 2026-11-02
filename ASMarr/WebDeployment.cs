using System.Text.RegularExpressions;

namespace ASMarr;

public static class WebDeployment
{
    public static string UrlBase(string? value)
    {
        if(string.IsNullOrEmpty(value)||value=="/")return "";
        if(!Regex.IsMatch(value,@"^(/[A-Za-z0-9_-]+)+$"))
            throw new InvalidOperationException("ASMARR_URL_BASE must be a path such as /asmarr without a trailing slash");
        return value;
    }
    public static string Index(string webRoot,string urlBase)=>
        File.ReadAllText(Path.Combine(webRoot,"index.html")).Replace("<base href=\"/\" />",$"<base href=\"{urlBase}/\" />");
}
