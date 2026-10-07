namespace ASMarr;

public static class CreatorLibrary
{
    public static List<Dictionary<string,object?>> Read(Store store)=>store.Query("""
        SELECT c.*,count(DISTINCT a.key) AS recordings,
          sum(CASE WHEN aa.state IN ('complete','imported') THEN 1 ELSE 0 END) AS audio_completed,
          sum(CASE WHEN va.state='imported' THEN 1 ELSE 0 END) AS video_completed
        FROM creators c LEFT JOIN assets a ON a.creator=c.name
        LEFT JOIN media_assets aa ON aa.recording_key=a.key AND aa.media_kind='Audio'
        LEFT JOIN media_assets va ON va.recording_key=a.key AND va.media_kind='Video'
        GROUP BY c.id ORDER BY c.name COLLATE NOCASE
        """);
}
