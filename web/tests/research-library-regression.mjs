import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile } from "node:fs/promises";
import { resolve, extname, relative, isAbsolute } from "node:path";
import { chromium, expect } from "@playwright/test";
const webRoot=resolve(process.argv[2]);
const evidence=resolve(process.argv[3]);
await mkdir(evidence,{recursive:true});
const snapshot=JSON.parse(await readFile(new URL("./snapshot-before.json",import.meta.url),"utf8"));
snapshot.human_collaboration.human_requests.items=[];
snapshot.human_collaboration.human_requests.waiting={scope:"none",safe_meaningful_runnable_exists:true};
let language="zh"; let input=null; let failContentOnce=true;
const requests=[]; const errors=[];
const server=createServer(async(req,res)=>{
  const u=new URL(req.url,"http://fixture");
  const send=(body,status=200)=>{res.writeHead(status,{"Content-Type":"application/json","Set-Cookie":"meta_research_csrf=fixture; Path=/"});res.end(JSON.stringify(body));};
  if(u.pathname.startsWith("/api/")){
    requests.push(u.pathname+u.search);
    if(u.pathname==="/api/v1/preferences"){
      if(req.method==="PUT"){let body="";for await(const chunk of req) body+=chunk;language=JSON.parse(body).output_language;assert.equal(req.headers["x-csrf-token"],"fixture");}
      return send({output_language:language});
    }
    if(u.pathname.startsWith("/api/v1/research-library/")){
      const entry=u.pathname.split("/").at(-1);
      const offset=Number(u.searchParams.get("offset")??0);
      if(entry==="human"&&u.searchParams.has("request_cursor"))return send({items:[{ref:"later-response",name:"Later request response",summary:"A second page of human guidance."}],next_offset:null,request_next_cursor:null});
      if(entry==="questions") return send({items:u.searchParams.has("question_ref")
        ?[{ref:"outcome-"+offset,name:"Preserved conclusion "+offset,summary:"A previous accepted research finding.",reader:{source_ref:"outcome-"+offset,version_ref:"outcome-"+offset}}]
        :[{question_ref:"question-library-"+offset,name:"questions material "+offset,reader:{source_ref:"question-library-"+offset,version_ref:"question-content-"+offset},history_reader:{operation:"research_graph.question_history.read",question_ref:"question-library-"+offset,offset:0,limit:12}}],next_offset:offset===0?12:null});
      if(entry==="baselines"){
        const items=u.searchParams.has("variant_ref")?[{baseline_ref:"b1",variant_ref:"v1",name:"Selected method",runs:[{variant_run_ref:"run1",name:"A real method run",reader:{operation:"research_graph.formal_results.read",ref:"run1"}}]}]:u.searchParams.has("baseline_ref")?[{baseline_ref:"b1",variant_ref:"v1",name:"Selected method"}]:[{baseline_ref:"b1",name:"Reference method"}];
        return send({items,next_offset:null});
      }
      if(entry==="datasets") return send({items:u.searchParams.has("dataset_ref")?[{dataset_ref:"dataset_test",dataset_version_ref:"dv1",name:"Data version one",readers:[{source_ref:"dv1",version_ref:"asset_v1"}],reader:{source_ref:"dv1",version_ref:"asset_v1"}}]:[{dataset_ref:"dataset_test",name:"Comparison measurements",summary:"Two measured values."}],next_offset:null});
      if(entry==="environments") return send({items:u.searchParams.has("environment_ref")
        ?[{environment_reference_ref:"use-"+offset,environment_ref:u.searchParams.get("environment_ref"),question_ref:"question-library",research_ref:"run-calibration",summary:"Calibration use "+offset}]
        :[{environment_ref:"environment-room",name:"Existing imaging room",summary:"An available physical imaging facility.",source:"Building A, room 12",notes:"Book a supervised session.",metadata:{access:"Weekday booking"},asset_bindings:[],readers:[]},
          {environment_ref:"environment-simulator",name:"Reusable simulator",summary:"A simulation environment with exact preserved instructions.",source:"Accepted simulation package",reader:{source_ref:"environment-simulator",version_ref:"asset-env-manual"},readers:[{source_ref:"environment-simulator",version_ref:"asset-env-manual"},{source_ref:"environment-simulator",version_ref:"asset-env-config"}]}],next_offset:u.searchParams.has("environment_ref")&&offset===0?12:null});
      return send({items:[{ref:entry+"-"+offset,name:entry+" material "+offset,summary:"A saved research observation.",reader:{source_ref:entry+"-source",version_ref:"precise-v1"}}],next_offset:offset===0?12:null,request_next_cursor:entry==="human"?"request-page-2":null});
    }
    if(u.pathname==="/api/v1/research-formal")return send({result:{variant_run_ref:"run1"},items:[{ref:"artifact1",name:"Preserved run output",reader:{source_ref:"run-output",version_ref:"exact-run-v1"}}],next_offset:null});
    if(u.pathname==="/api/v1/research-content"){
      assert.ok(u.searchParams.get("source_ref")&&!['undefined','null'].includes(u.searchParams.get("source_ref")));
      assert.ok(u.searchParams.get("version_ref"));
      if(u.searchParams.get("source_ref").startsWith("outcome-")) return send({text:JSON.stringify({claim:"The accepted conclusion remains readable.",support_scope:"Only the retained measurements."}),next_offset:null,complete:true});
      if(u.searchParams.get("source_ref")==="environment-simulator") {
        const version=u.searchParams.get("version_ref");assert.ok(["asset-env-manual","asset-env-config"].includes(version));
        return send({text:version==="asset-env-manual"?"Exact simulator manual.":"Exact simulator configuration.",next_offset:null,complete:true});
      }
      if(u.searchParams.get("source_ref")==="literature-source"&&u.searchParams.get("offset")==="26"&&failContentOnce){failContentOnce=false;return send({detail:{code:"temporary_read_failure"}},503);}
      if(u.searchParams.get("source_ref")==="dv1"&&!u.searchParams.has("entry_path")) return send({kind:"directory",entries:[{path:"measurements.txt",size:26}],next_offset:null});
      if(u.searchParams.has("entry_path")) return send({text:"Dataset original contents.",next_offset:26,complete:true});
      return send({text:Number(u.searchParams.get("offset"))===0?"Exact original paragraph. ":"Continuation of the original.",next_offset:Number(u.searchParams.get("offset"))===0?26:null});
    }
    if(u.pathname==="/api/v1/research-inputs"){
      let body="";for await(const chunk of req) body+=chunk;input=JSON.parse(body);
      assert.equal(req.headers["x-csrf-token"],"fixture");assert.ok(req.headers["idempotency-key"]);
      return send({status:"accepted",input_ref:"test-human-input"});
    }
    if(u.pathname==="/api/v1/snapshot")return send(snapshot);
    if(u.pathname==="/api/v1/research-assets")return send(snapshot.research_assets);
    if(u.pathname==="/api/v1/status")return send({schema_ref:"meta-research/runtime-status/v1",revision:1,observed_at:new Date().toISOString(),updated_at:new Date().toISOString(),state:"waiting",current_task:null,waiting_reason:null,pending_requests:0,foreground:snapshot.research_control.foreground,health:{status:"ready",checks:[]}});
    if(u.pathname==="/api/v1/research-overview")return send({schema_ref:"meta-research/research-overview/v1",status:"ready",foreground:snapshot.research_control.foreground,findings:{quest:[],question:[],cycle:[]},cycles:[],reason:null});
    if(u.pathname.includes("projection")||u.pathname.includes("events")){res.writeHead(200,{"Content-Type":"text/event-stream"});res.write(": fixture\n\n");return;}
    return send({},404);
  }
  try{const file=resolve(webRoot,u.pathname.startsWith("/assets/")?"."+u.pathname:"index.html");const path=relative(webRoot,file);assert.ok(path&&!path.startsWith("..")&&!isAbsolute(path));res.setHeader("Content-Type",{".js":"application/javascript",".css":"text/css",".html":"text/html"}[extname(file)]??"application/octet-stream");res.end(await readFile(file));}catch{res.writeHead(404).end();}
});
await new Promise(done=>server.listen(0,"127.0.0.1",done));
const base=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({executablePath:process.env.META_RESEARCH_CHROME??"/root/.cache/ms-playwright/chromium-1193/chrome-linux/chrome",headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
await context.route("**/*",r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
const page=await context.newPage();page.on("pageerror",e=>errors.push(e.message));
try{
  await page.goto(base+"/?panel=research-assets");
  const dialog=page.locator(".asset-dialog");
  await expect(dialog.getByRole("tab",{name:"研究问题",exact:true})).toBeVisible();
  await expect(dialog.getByText("questions material 0",{exact:true})).toBeVisible();
  await dialog.getByRole("button",{name:"下一页",exact:true}).click();
  await expect(dialog.getByText("questions material 12",{exact:true})).toBeVisible();
  await dialog.getByRole("button",{name:"阅读研究历史",exact:true}).click();
  await expect(dialog.getByText("Preserved conclusion 0",{exact:true})).toBeVisible();
  await dialog.getByRole("button",{name:"下一页",exact:true}).click();
  await expect(dialog.getByText("Preserved conclusion 12",{exact:true})).toBeVisible();
  await dialog.getByRole("button",{name:"阅读原文",exact:true}).click();
  await expect(dialog.getByText("The accepted conclusion remains readable.",{exact:true})).toBeVisible();
  await dialog.getByRole("button",{name:"返回上一级",exact:true}).click();
  await expect(dialog.getByText("questions material 0",{exact:true})).toBeVisible();
  await dialog.getByRole("tab",{name:"研究方法",exact:true}).click();
  await dialog.getByRole("button",{name:"查看方法变体",exact:true}).click();
  await dialog.getByRole("button",{name:"查看实施与评价",exact:true}).click();
  await dialog.getByRole("button",{name:"查看实施产物",exact:true}).click();
  await expect(dialog.getByText("Preserved run output",{exact:true})).toBeVisible();
  await dialog.getByRole("button",{name:"阅读原文",exact:true}).click();
  await expect(dialog.getByText("Exact original paragraph.",{exact:false})).toBeVisible();
  await dialog.getByRole("tab",{name:"数据集",exact:true}).click();
  await dialog.getByRole("button",{name:"查看数据版本",exact:true}).click();
  await expect(dialog.getByText("Data version one",{exact:true})).toBeVisible();
  await dialog.getByRole("button",{name:"阅读原文",exact:true}).click();
  await dialog.getByRole("button",{name:"measurements.txt",exact:true}).click();
  await expect(dialog.getByText("Dataset original contents.",{exact:true})).toBeVisible();
  await expect(dialog.getByRole("button",{name:"继续读取",exact:true})).toHaveCount(0);
  await dialog.getByRole("tab",{name:"文献",exact:true}).click();
  await expect(dialog.getByText("literature material 0",{exact:true})).toBeVisible();
  await dialog.getByRole("button",{name:"阅读原文",exact:true}).click();
  await expect(dialog.getByText("Exact original paragraph.",{exact:false})).toBeVisible();
  await dialog.getByRole("button",{name:"继续读取",exact:true}).click();
  await expect(dialog.getByText("原文暂时不可读。已读内容保留，可重试。",{exact:true})).toBeVisible();
  await expect(dialog.getByText("Exact original paragraph.",{exact:false})).toBeVisible();
  await dialog.getByRole("button",{name:"重试读取",exact:true}).click();
  await expect(dialog.getByText("Continuation of the original.",{exact:false})).toBeVisible();
  await dialog.getByRole("tab",{name:"环境与资源",exact:true}).click();
  const physical=dialog.locator(".library-card").filter({hasText:"Existing imaging room"});
  await expect(physical.getByText("Book a supervised session.",{exact:true})).toBeVisible();
  await expect(physical.getByRole("button",{name:"阅读说明或材料",exact:true})).toHaveCount(0);
  await expect(physical.getByText("这是资源信息，未关联数字材料。可根据位置、来源与使用条件采用。",{exact:true})).toBeVisible();
  await physical.getByText("能力与已知使用条件",{exact:true}).click();
  await expect(physical.getByText(/Weekday booking/).first()).toBeVisible();
  await physical.getByRole("button",{name:"查看用途与研究关联",exact:true}).click();
  await expect(dialog.getByText("Calibration use 0",{exact:true})).toBeVisible();
  await expect(dialog.getByText("研究问题：",{exact:true})).toBeVisible();
  await expect(dialog.getByRole("button",{name:"阅读说明或材料",exact:true})).toHaveCount(0);
  await expect(dialog.getByText("这是资源信息，未关联数字材料。可根据位置、来源与使用条件采用。",{exact:true})).toHaveCount(0);
  await dialog.getByRole("button",{name:"下一页",exact:true}).click();
  await expect(dialog.getByText("Calibration use 12",{exact:true})).toBeVisible();
  await dialog.getByRole("button",{name:"返回上一级",exact:true}).click();
  const digital=dialog.locator(".library-card").filter({hasText:"Reusable simulator"});
  await digital.getByRole("button",{name:"阅读说明或材料",exact:true}).click();
  await expect(dialog.getByText("Exact simulator manual.",{exact:true})).toBeVisible();
  await digital.getByText("此环境关联的所有材料",{exact:true}).click();
  await digital.getByRole("button",{name:"读取材料 2",exact:true}).click();
  await expect(dialog.getByText("Exact simulator configuration.",{exact:true})).toBeVisible();
  await dialog.getByRole("combobox",{name:"输出语言 / Output language"}).selectOption("en");
  await expect(dialog.getByRole("tab",{name:"Human input",exact:true})).toBeVisible();
  await dialog.getByRole("tab",{name:"Human input",exact:true}).click();
  await dialog.getByRole("button",{name:"More request responses",exact:true}).click();
  await expect(dialog.getByText("Later request response",{exact:true})).toBeVisible();
  await dialog.getByLabel("Research guidance").fill("Test user guidance: compare uncertainty before choosing a method.");
  await dialog.getByRole("button",{name:"Save guidance",exact:true}).click();
  await expect(dialog.getByText("Saved. Future research can discover this guidance.",{exact:true})).toBeVisible();
  assert.equal(input.text,"Test user guidance: compare uncertainty before choosing a method.");
  assert.ok(input.quest_ref);assert.equal(language,"en");
  await page.screenshot({path:evidence+"/library-desktop.png"});
  await page.setViewportSize({width:390,height:844});
  await expect(dialog).toBeVisible();
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:evidence+"/library-mobile.png"});
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({status:"passed",checks:["six-entry-navigation","independent-pagination","question-research-history-pages-and-original","exact-content-continuation","baseline-variant-run-output","dataset-version-directory-file","environment-physical-resource-without-digital-material","environment-research-use-pagination","environment-multiple-exact-material-bindings","failed-content-retains-read-pages-and-retries","language-save-and-render","human-input-csrf-idempotency","independent-human-response-cursor","desktop-mobile-no-overflow"],requests:requests.filter(x=>x.includes("research-library")||x.includes("research-content")||x.includes("research-formal"))}));
}finally{await browser.close();server.closeAllConnections();await new Promise(done=>server.close(done));}
