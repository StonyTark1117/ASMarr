using Microsoft.Data.Sqlite;
using System.Text.Json;

namespace ASMarr;

public sealed class Store
{
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
        CREATE TABLE IF NOT EXISTS creators(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,aliases TEXT NOT NULL DEFAULT '[]',path TEXT NOT NULL,tags TEXT NOT NULL DEFAULT '[]',monitored INTEGER NOT NULL DEFAULT 1,profile_id INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS identities(id INTEGER PRIMARY KEY,creator_id INTEGER REFERENCES creators(id),kind TEXT NOT NULL,handle TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,UNIQUE(creator_id,kind,handle));
        CREATE TABLE IF NOT EXISTS profiles(id INTEGER PRIMARY KEY,name TEXT NOT NULL,settings TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,name TEXT NOT NULL,arguments TEXT NOT NULL,state TEXT NOT NULL,created TEXT NOT NULL,started TEXT,finished TEXT,result TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS tasks(name TEXT PRIMARY KEY,interval_seconds INTEGER NOT NULL,next_run TEXT NOT NULL,last_run TEXT,last_result TEXT,enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS task_locks(name TEXT PRIMARY KEY,owner TEXT NOT NULL,expires TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,at TEXT NOT NULL,event TEXT NOT NULL,recording_key TEXT,details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS queue(id TEXT PRIMARY KEY,recording_key TEXT NOT NULL,download_id TEXT UNIQUE,state TEXT NOT NULL,provider TEXT NOT NULL,details TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS blocklist(id INTEGER PRIMARY KEY,recording_key TEXT,download_id TEXT,reason TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY,at TEXT NOT NULL,level TEXT NOT NULL,message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS shadow_cycles(id INTEGER PRIMARY KEY,started TEXT NOT NULL,finished TEXT,result TEXT NOT NULL,clean INTEGER NOT NULL DEFAULT 0,comparison TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS migration_audits(id INTEGER PRIMARY KEY,at TEXT NOT NULL,result TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS assets_state ON assets(state,retry_after);
        CREATE INDEX IF NOT EXISTS assets_creator ON assets(creator);
        INSERT OR IGNORE INTO schema_migrations VALUES(1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        """; c.ExecuteNonQuery();
        SettingDefault("mode", "shadow"); SettingDefault("naming", "{Creator}/Singles/{Title} [{SourceId}].{ext}");
        SettingDefault("root", "/mnt/cephfs/media/asmr");
        foreach (var (name, seconds) in new[] { ("discovery",86400), ("queue",300), ("disk-scan",86400), ("plex",600), ("playlists",86400), ("health",300), ("backup",86400) })
            Execute("INSERT OR IGNORE INTO tasks(name,interval_seconds,next_run) VALUES($name,$seconds,$next)", ("name", name), ("seconds", seconds), ("next", DateTimeOffset.UtcNow.AddSeconds(name == "discovery" ? 60 : seconds).ToString("O")));
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
    }
    public string Enqueue(string name, object arguments)
    {
        string id=Guid.NewGuid().ToString("N");
        Execute("INSERT INTO commands(id,name,arguments,state,created) VALUES($id,$name,$args,'queued',$at)",("id",id),("name",name),("args",JsonSerializer.Serialize(arguments)),("at",DateTimeOffset.UtcNow.ToString("O")));
        return id;
    }
    public string Backup()
    {
        string path=System.IO.Path.Combine(Root,"backups",$"asmarr-{DateTimeOffset.UtcNow:yyyyMMddTHHmmssfffZ}.db");
        using var source=Open(); using var dest=new SqliteConnection($"Data Source={path}"); dest.Open(); source.BackupDatabase(dest); return path;
    }
}
