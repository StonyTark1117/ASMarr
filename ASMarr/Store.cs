using Microsoft.Data.Sqlite;
using System.Text.Json;

namespace ASMarr;

public sealed class Store
{
    readonly object logLock=new();
    public string Root { get; } = Environment.GetEnvironmentVariable("ASMARR_STATE") ?? "/var/lib/asmarr";
    public string ConfigRoot { get; } = Environment.GetEnvironmentVariable("ASMARR_CONFIG") ?? "/etc/asmarr";
    public string Path => System.IO.Path.Combine(Root, "asmarr.db");
    public SqliteConnection Open()
    {
        var db = new SqliteConnection(new SqliteConnectionStringBuilder { DataSource = Path, DefaultTimeout = 30 }.ToString());
        db.Open();
        using var c = db.CreateCommand(); c.CommandText = "PRAGMA foreign_keys=ON; PRAGMA busy_timeout=30000;"; c.ExecuteNonQuery();
        return db;
    }
    public void Initialize()
    {
        Directory.CreateDirectory(Root); Directory.CreateDirectory(ConfigRoot);
        Directory.CreateDirectory(System.IO.Path.Combine(Root, "backups"));
        Directory.CreateDirectory(System.IO.Path.Combine(Root, "logs"));
        using var db = Open(); using var c = db.CreateCommand();
        c.CommandText = """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS downloads(id TEXT PRIMARY KEY,source TEXT,url TEXT,creator TEXT,title TEXT,saved_path TEXT,ts INTEGER);
        CREATE TABLE IF NOT EXISTS assets(key TEXT PRIMARY KEY,url TEXT,targets TEXT,source TEXT,creator TEXT,title TEXT,published INTEGER,saved_path TEXT,state TEXT,attempts INTEGER DEFAULT 0,retry_after INTEGER DEFAULT 0,error TEXT,acquired INTEGER);
        CREATE TABLE IF NOT EXISTS aliases(id TEXT PRIMARY KEY,asset_key TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS sources(name TEXT PRIMARY KEY,status TEXT,last_attempt INTEGER,last_success INTEGER,last_download INTEGER,details TEXT);
        CREATE TABLE IF NOT EXISTS runs(started INTEGER PRIMARY KEY,finished INTEGER,status TEXT,details TEXT);
        CREATE TABLE IF NOT EXISTS creators(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,aliases TEXT NOT NULL DEFAULT '[]',path TEXT NOT NULL,tags TEXT NOT NULL DEFAULT '[]',monitored INTEGER NOT NULL DEFAULT 1,profile_id INTEGER NOT NULL DEFAULT 1,monitor_video INTEGER NOT NULL DEFAULT 0,video_quality_profile_id INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS identities(id INTEGER PRIMARY KEY,creator_id INTEGER REFERENCES creators(id),kind TEXT NOT NULL,handle TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,UNIQUE(creator_id,kind,handle));
        CREATE TABLE IF NOT EXISTS profiles(id INTEGER PRIMARY KEY,name TEXT NOT NULL,settings TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS video_quality_profiles(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,resolution TEXT NOT NULL,settings TEXT NOT NULL,is_builtin INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS media_assets(recording_key TEXT NOT NULL REFERENCES assets(key) ON DELETE CASCADE,media_kind TEXT NOT NULL CHECK(media_kind IN ('Audio','Video')),wanted INTEGER NOT NULL DEFAULT 1,state TEXT NOT NULL DEFAULT 'wanted',saved_path TEXT,source_url TEXT,provider_id TEXT,attempts INTEGER NOT NULL DEFAULT 0,retry_after INTEGER NOT NULL DEFAULT 0,error TEXT,acquired INTEGER,details TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(recording_key,media_kind));
        CREATE TABLE IF NOT EXISTS video_candidates(id INTEGER PRIMARY KEY,recording_key TEXT NOT NULL REFERENCES assets(key) ON DELETE CASCADE,provider TEXT NOT NULL,provider_id TEXT,url TEXT NOT NULL,normalized_url TEXT NOT NULL,fingerprint TEXT,resolution INTEGER,source_quality INTEGER NOT NULL DEFAULT 0,bitrate INTEGER NOT NULL DEFAULT 0,video_codec TEXT,audio_codec TEXT,container TEXT,requires_transcode INTEGER NOT NULL DEFAULT 0,interactive_only INTEGER NOT NULL DEFAULT 0,details TEXT NOT NULL DEFAULT '{}',UNIQUE(provider,provider_id),UNIQUE(normalized_url));
        CREATE TABLE IF NOT EXISTS backfill_jobs(id TEXT PRIMARY KEY,creator_id INTEGER NOT NULL REFERENCES creators(id),media_kind TEXT NOT NULL,state TEXT NOT NULL,discovered INTEGER NOT NULL DEFAULT 0,eligible INTEGER NOT NULL DEFAULT 0,total INTEGER NOT NULL DEFAULT 0,cursor TEXT,started TEXT NOT NULL,updated TEXT NOT NULL,finished TEXT,error TEXT);
        CREATE TRIGGER IF NOT EXISTS assets_media_audio_insert AFTER INSERT ON assets BEGIN
          INSERT OR IGNORE INTO media_assets(recording_key,media_kind,wanted,state,saved_path,attempts,retry_after,error,acquired)
          VALUES(NEW.key,'Audio',CASE WHEN NEW.state='suppressed' THEN 0 ELSE 1 END,CASE NEW.state WHEN 'pending' THEN 'wanted' ELSE NEW.state END,NEW.saved_path,NEW.attempts,NEW.retry_after,NEW.error,NEW.acquired);
        END;
        CREATE TRIGGER IF NOT EXISTS assets_media_audio_update AFTER UPDATE OF state,saved_path,attempts,retry_after,error,acquired ON assets BEGIN
          UPDATE media_assets SET wanted=CASE WHEN NEW.state='suppressed' THEN 0 ELSE wanted END,
            state=CASE NEW.state WHEN 'pending' THEN 'wanted' ELSE NEW.state END,saved_path=NEW.saved_path,
            attempts=NEW.attempts,retry_after=NEW.retry_after,error=NEW.error,acquired=NEW.acquired
          WHERE recording_key=NEW.key AND media_kind='Audio';
        END;
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,name TEXT NOT NULL,arguments TEXT NOT NULL,state TEXT NOT NULL,created TEXT NOT NULL,started TEXT,finished TEXT,result TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS tasks(name TEXT PRIMARY KEY,interval_seconds INTEGER NOT NULL,next_run TEXT NOT NULL,last_run TEXT,last_result TEXT,enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS task_locks(name TEXT PRIMARY KEY,owner TEXT NOT NULL,expires TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,at TEXT NOT NULL,event TEXT NOT NULL,recording_key TEXT,details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS queue(id TEXT PRIMARY KEY,recording_key TEXT NOT NULL,download_id TEXT UNIQUE,state TEXT NOT NULL,provider TEXT NOT NULL,details TEXT NOT NULL,created TEXT NOT NULL,media_kind TEXT NOT NULL DEFAULT 'Audio');
        CREATE TABLE IF NOT EXISTS blocklist(id INTEGER PRIMARY KEY,recording_key TEXT,download_id TEXT,reason TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY,at TEXT NOT NULL,level TEXT NOT NULL,message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS shadow_cycles(id INTEGER PRIMARY KEY,started TEXT NOT NULL,finished TEXT,result TEXT NOT NULL,clean INTEGER NOT NULL DEFAULT 0,comparison TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS migration_audits(id INTEGER PRIMARY KEY,at TEXT NOT NULL,result TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS assets_state ON assets(state,retry_after);
        CREATE INDEX IF NOT EXISTS assets_creator ON assets(creator);
        CREATE INDEX IF NOT EXISTS media_assets_state ON media_assets(media_kind,state,retry_after);
        CREATE INDEX IF NOT EXISTS video_candidates_recording ON video_candidates(recording_key);
        INSERT OR IGNORE INTO schema_migrations VALUES(1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        """; c.ExecuteNonQuery();
        AddColumn(db,"creators","monitor_video","INTEGER NOT NULL DEFAULT 0");
        AddColumn(db,"creators","video_quality_profile_id","INTEGER NOT NULL DEFAULT 1");
        AddColumn(db,"queue","media_kind","TEXT NOT NULL DEFAULT 'Audio'");
        // Existing deployments become audio assets only. The video opt-in remains
        // false, so applying this migration can never initiate video work.
        using(var migrate=db.CreateCommand())
        {
            migrate.CommandText="""
            INSERT OR IGNORE INTO video_quality_profiles(id,name,resolution,settings,is_builtin) VALUES
              (1,'Any / highest preferred','Any','{"preferHighest":true}',1),
              (2,'2160p','2160p','{}',1),(3,'1440p','1440p','{}',1),
              (4,'1080p','1080p','{}',1),(5,'720p','720p','{}',1),(6,'480p','480p','{}',1);
            INSERT OR IGNORE INTO media_assets(recording_key,media_kind,wanted,state,saved_path,attempts,retry_after,error,acquired)
              SELECT key,'Audio',CASE WHEN state='suppressed' THEN 0 ELSE 1 END,
                CASE state WHEN 'pending' THEN 'wanted' ELSE state END,saved_path,attempts,retry_after,error,acquired FROM assets;
            INSERT OR IGNORE INTO schema_migrations VALUES(2,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
            """;
            migrate.ExecuteNonQuery();
        }
        SettingDefault("mode", "shadow"); SettingDefault("naming", "{Creator}/Singles/{Title} [{SourceId}].{ext}");
        SettingDefault("root", "/mnt/cephfs/media/asmr");
        SettingDefault("video.root", "/mnt/cephfs/media/asmr-video");
        SettingDefault("video.free_space_gib", "20");
        SettingDefault("video.concurrency", "1");
        SettingDefault("video.naming", "{Creator}/{Year}/{Date} - {Title} [{Provider}-{SourceId}].{ext}");
        foreach (var (name, seconds) in new[] { ("discovery",86400), ("queue",300), ("video-queue",300), ("disk-scan",86400), ("plex",600), ("plex-video",600), ("playlists",86400), ("health",300), ("backup",86400) })
            Execute("INSERT OR IGNORE INTO tasks(name,interval_seconds,next_run) VALUES($name,$seconds,$next)", ("name", name), ("seconds", seconds), ("next", DateTimeOffset.UtcNow.AddSeconds(name == "discovery" ? 60 : seconds).ToString("O")));
    }
    static void AddColumn(SqliteConnection db,string table,string column,string definition)
    {
        using var inspect=db.CreateCommand();inspect.CommandText=$"PRAGMA table_info({table})";
        using var reader=inspect.ExecuteReader();bool exists=false;
        while(reader.Read())if(string.Equals(reader.GetString(1),column,StringComparison.OrdinalIgnoreCase)){exists=true;break;}
        reader.Close();
        if(exists)return;
        using var alter=db.CreateCommand();alter.CommandText=$"ALTER TABLE {table} ADD COLUMN {column} {definition}";alter.ExecuteNonQuery();
    }
    public List<Dictionary<string,object?>> Query(string sql, params (string, object?)[] args)
    {
        using var db = Open(); using var c = db.CreateCommand(); c.CommandText = sql; Bind(c,args);
        using var r = c.ExecuteReader(); var rows = new List<Dictionary<string,object?>>();
        while (r.Read()) { var row = new Dictionary<string,object?>(); for (int i=0;i<r.FieldCount;i++) row[r.GetName(i)] = r.IsDBNull(i) ? null : r.GetValue(i); rows.Add(row); }
        return rows;
    }
    public int Execute(string sql, params (string, object?)[] args)
    { using var db = Open(); using var c = db.CreateCommand(); c.CommandText=sql; Bind(c,args); return c.ExecuteNonQuery(); }
    static void Bind(SqliteCommand c, (string,object?)[] args) { foreach (var (k,v) in args) c.Parameters.AddWithValue("$"+k,v??DBNull.Value); }
    public string Setting(string key, string fallback="") => Query("SELECT value FROM settings WHERE key=$key", ("key",key)).FirstOrDefault()?.GetValueOrDefault("value")?.ToString() ?? fallback;
    public void Set(string key,string value) => Execute("INSERT INTO settings VALUES($key,$value) ON CONFLICT(key) DO UPDATE SET value=excluded.value",("key",key),("value",value));
    void SettingDefault(string key,string value) => Execute("INSERT OR IGNORE INTO settings VALUES($key,$value)",("key",key),("value",value));
    public void Log(string level,string message)
    {
        Execute("INSERT INTO logs(at,level,message) VALUES($at,$level,$message)",("at",DateTimeOffset.UtcNow.ToString("O")),("level",level),("message",message));
        lock(logLock)File.AppendAllText(System.IO.Path.Combine(Root,"logs","asmarr.jsonl"),JsonSerializer.Serialize(new {at=DateTimeOffset.UtcNow,level,message})+"\n");
    }
    public string Enqueue(string name, object arguments)
    {
        string id=Guid.NewGuid().ToString("N");
        Execute("INSERT INTO commands(id,name,arguments,state,created) VALUES($id,$name,$args,'queued',$at)",("id",id),("name",name),("args",JsonSerializer.Serialize(arguments)),("at",DateTimeOffset.UtcNow.ToString("O")));
        return id;
    }
    public void InterruptRunningVideoBackfills()
    {
        var now=DateTimeOffset.UtcNow.ToString("O");
        Execute("UPDATE backfill_jobs SET state='interrupted',updated=$now,error='Service restarted during history scan' WHERE media_kind='Video' AND state='running'",("now",now));
    }
    public int EnqueueResumableVideoBackfills(TimeSpan failedRetryDelay)
    {
        var cutoff=DateTimeOffset.UtcNow.Subtract(failedRetryDelay).ToString("O");
        int queued=0;
        foreach(var job in Query("""
            SELECT b.creator_id FROM backfill_jobs b JOIN creators c ON c.id=b.creator_id
            WHERE b.media_kind='Video' AND c.monitor_video=1
              AND (b.state='interrupted' OR (b.state='failed' AND b.updated<=$cutoff))
            ORDER BY b.updated
            """,("cutoff",cutoff)))
        {
            int creatorId=Convert.ToInt32(job["creator_id"]);
            if(Query("""
                SELECT id FROM commands WHERE name='video-backfill' AND state IN ('queued','running')
                  AND CAST(json_extract(arguments,'$.creatorId') AS INTEGER)=$creator
                LIMIT 1
                """,("creator",creatorId)).Count>0)continue;
            Enqueue("video-backfill",new {creatorId});queued++;
        }
        return queued;
    }
    public string Backup()
    {
        string path=System.IO.Path.Combine(Root,"backups",$"asmarr-{DateTimeOffset.UtcNow:yyyyMMddTHHmmssfffZ}.db");
        using var source=Open(); using var dest=new SqliteConnection($"Data Source={path}"); dest.Open(); source.BackupDatabase(dest); return path;
    }
    public bool TryAcquireLease(string name,string owner,TimeSpan ttl)
    {
        var now=DateTimeOffset.UtcNow;
        return Execute("INSERT INTO task_locks(name,owner,expires) VALUES($name,$owner,$expiry) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires=excluded.expires WHERE task_locks.expires<$now",("name",name),("owner",owner),("expiry",now.Add(ttl).ToString("O")),("now",now.ToString("O")))==1;
    }
    public bool RenewLease(string name,string owner,TimeSpan ttl)=>Execute("UPDATE task_locks SET expires=$expiry WHERE name=$name AND owner=$owner",("name",name),("owner",owner),("expiry",DateTimeOffset.UtcNow.Add(ttl).ToString("O")))==1;
    public void ReleaseLease(string name,string owner)=>Execute("DELETE FROM task_locks WHERE name=$name AND owner=$owner",("name",name),("owner",owner));
}
