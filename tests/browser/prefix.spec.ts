import { test, expect } from "@playwright/test";
import { mkdtempSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { spawn } from "node:child_process";

test("URL prefix scopes assets, login, API and live updates", async ({page,request}) => {
  const root=mkdtempSync(join(tmpdir(),"asmarr-prefix-browser-"));
  const state=join(root,"state"), config=join(root,"config");
  mkdirSync(state,{mode:0o700});mkdirSync(config,{mode:0o700});
  const fixture=dirname(JSON.parse(readFileSync("runtime/session.json","utf8")).config);
  const origin="https://127.0.0.1:8790", prefix=origin+"/fixture/";
  const app=resolve("../../publish");
  const child=spawn(join(app,"ASMarr"),[],{cwd:app,stdio:"ignore",env:{...process.env,
    ASMARR_STATE:state,ASMARR_CONFIG:config,ASMARR_MAINTENANCE_HOLD:"1",ASMARR_URL_BASE:"/fixture",
    ASPNETCORE_URLS:origin,ASPNETCORE_Kestrel__Certificates__Default__Path:join(fixture,"cert.pem"),
    ASPNETCORE_Kestrel__Certificates__Default__KeyPath:join(fixture,"key.pem")}});
  try {
    await expect.poll(async()=>{
      if(child.exitCode!==null)throw new Error("Prefix fixture exited");
      try{return(await request.get(prefix+"healthz")).status();}catch{return 0;}
    },{timeout:20000}).toBe(200);
    const paths:string[]=[];page.on("request",r=>{if(r.url().startsWith(origin))paths.push(new URL(r.url()).pathname);});
    await page.goto(prefix);
    await expect(page.getByRole("button",{name:"Sign in",exact:true})).toBeVisible();
    expect(await page.evaluate(()=>document.baseURI)).toBe(prefix);
    const password=readFileSync(join(config,"initial-admin.txt"),"utf8").split("\n")[1].split(": ")[1];
    await page.getByLabel("Password",{exact:true}).fill(password);
    await page.getByRole("button",{name:"Sign in",exact:true}).click();
    await expect(page.getByText("Connected to server",{exact:true})).toBeVisible();
    const session=(await page.context().cookies()).find(c=>c.name==="__Host-ASMarr");
    expect(session?.path).toBe("/");expect(session?.secure).toBe(true);
    expect(paths.some(p=>p.startsWith("/fixture/assets/"))).toBe(true);
    expect(paths.some(p=>p==="/fixture/api/v1/auth/login")).toBe(true);
    expect(paths.some(p=>p.startsWith("/fixture/api/v1/events"))).toBe(true);
    expect(paths.every(p=>p.startsWith("/fixture/"))).toBe(true);
    await page.getByTitle("Sign out").click();
    await expect(page.getByRole("button",{name:"Sign in",exact:true})).toBeVisible();
  } finally {
    child.kill("SIGTERM");
    if(child.exitCode===null)await new Promise<void>(resolve=>child.once("exit",()=>resolve()));
    rmSync(root,{recursive:true,force:true});
  }
});
