using ASMarr;
using Microsoft.Data.Sqlite;

var root=Path.Combine(Path.GetTempPath(),"asmarr-store-tests-"+Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(root);
Environment.SetEnvironmentVariable("ASMARR_STATE",Path.Combine(root,"state"));
Environment.SetEnvironmentVariable("ASMARR_CONFIG",Path.Combine(root,"config"));
var store=new Store();store.Initialize();store.Initialize();
int assertions=0;
void Assert(bool condition,string name){if(!condition)throw new InvalidOperationException(name);assertions++;Console.WriteLine("PASS "+name);}
Assert(store.Query("PRAGMA integrity_check")[0].Values.First()!.ToString()=="ok","migrations retain database integrity");
Assert(store.Query("PRAGMA journal_mode")[0].Values.First()!.ToString()=="wal","database uses WAL");
Assert(store.Query("SELECT * FROM schema_migrations").Count==1,"repeat migration is idempotent");
Assert(store.Query("SELECT * FROM tasks").Count==7,"repeat migration does not duplicate tasks");
Assert(store.Setting("mode")=="shadow","first boot gates production writes");
var leases=await Task.WhenAll(Enumerable.Range(0,10).Select(i=>Task.Run(()=>store.TryAcquireLease("test-provider",i.ToString(),TimeSpan.FromMinutes(1)))));
Assert(leases.Count(x=>x)==1,"concurrent tasks have exactly one lease holder");
string owner=store.Query("SELECT owner FROM task_locks WHERE name='test-provider'")[0]["owner"]!.ToString()!;
Assert(!store.RenewLease("test-provider","wrong-owner",TimeSpan.FromMinutes(1)),"another owner cannot renew lease");
store.ReleaseLease("test-provider","wrong-owner");
Assert(!store.TryAcquireLease("test-provider","next",TimeSpan.FromMinutes(1)),"another owner cannot release lease");
Assert(store.RenewLease("test-provider",owner,TimeSpan.FromMinutes(1)),"lease holder renews active execution");
store.Execute("UPDATE task_locks SET expires=$expired WHERE name='test-provider'",("expired",DateTimeOffset.UtcNow.AddMinutes(-1).ToString("O")));
Assert(store.TryAcquireLease("test-provider","replacement",TimeSpan.FromMinutes(1)),"expired leases recover after interruption");
store.ReleaseLease("test-provider",owner);
Assert(store.Query("SELECT owner FROM task_locks WHERE name='test-provider'")[0]["owner"]!.ToString()=="replacement","former owner cannot release replacement lease");
var id=store.Enqueue("health",new {test=true});
var reopened=new Store();
Assert(reopened.Query("SELECT state FROM commands WHERE id=$id",("id",id))[0]["state"]!.ToString()=="queued","commands persist across connection lifetime");
string backup=store.Backup();
using(var db=new SqliteConnection("Data Source="+backup)){db.Open();using var c=db.CreateCommand();c.CommandText="SELECT count(*) FROM commands";Assert(Convert.ToInt32(c.ExecuteScalar())==1,"SQLite online backup retains durable commands");}
Console.WriteLine($"{assertions} store integration assertions passed");
SqliteConnection.ClearAllPools();Directory.Delete(root,true);
