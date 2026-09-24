import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { chromium, expect } from '@playwright/test';
const base='http://127.0.0.1:8767';
const expectedIndex=await readFile(new URL('../../src/meta_research/web_dist/index.html',import.meta.url),'utf8');
const expectedAsset=expectedIndex.match(/assets\/index-[^"']+\.js/)?.[0];assert.ok(expectedAsset);
const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:960},serviceWorkers:'block'});
const writes=[],errors=[],responses=[];
await context.route('**/*',route=>{
  const request=route.request();const url=new URL(request.url());
  if(url.origin!==base)return route.abort();
  if(!['GET','HEAD'].includes(request.method())){writes.push(request.method()+' '+url.pathname);return route.abort();}
  return route.continue();
});
const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
page.on('response',response=>{const url=new URL(response.url());if(/\/experiment-logs|\/raw-output/.test(url.pathname))responses.push({path:url.pathname,status:response.status()});});
try{
  const session=await context.request.get(base+'/api/v1/session');assert.equal(session.status(),200,'fresh read-only browser session');
  await page.goto(base+'/?workspace=1',{waitUntil:'domcontentloaded',timeout:30000});
  await expect(page.getByTestId('product-shell')).toHaveAttribute('data-shell-state','ready-active',{timeout:45000});
  const selector=page.getByRole('combobox',{name:'选择实验任务',exact:true});await expect(selector).toBeVisible();
  const t5=await selector.locator('option').evaluateAll(options=>options.filter(option=>option.textContent?.startsWith('T5')).map(option=>({value:option.value,label:option.textContent})));
  assert.equal(t5.length,1,'exactly one T5 selection');await selector.selectOption(t5[0].value);
  await page.getByRole('button',{name:/⌘ 实验日志/}).click();
  const files=page.locator('#experiment-log-dialog');await expect(files).toBeVisible();
  await expect(files.getByRole('button',{name:'任务执行记录',exact:true})).toBeVisible();
  await files.getByRole('button',{name:'任务执行记录',exact:true}).click();
  const records=page.locator('#target-output-dialog');await expect(records).toBeVisible();
  await expect(records.locator('header')).toContainText('T5');
  await expect(records.getByRole('log')).not.toBeEmpty({timeout:20000});
  const visibleRecordCharacters=(await records.getByRole('log').innerText()).length;
  const title=await records.locator('header b').innerText();
  const recordChannelLabel=await records.locator('.lumen-target-terminal-actions > span').innerText();
  const recordStatus=await records.getAttribute('data-terminal-state');
  await expect(records.getByRole('button',{name:'日志文件',exact:true})).toBeVisible();
  await page.setViewportSize({width:390,height:844});
  await expect(records.getByRole('button',{name:'日志文件',exact:true})).toBeVisible();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await records.getByRole('button',{name:'日志文件',exact:true}).click();
  await expect(files).toBeVisible();
  await expect(files.locator('.experiment-log-states')).not.toContainText('正在连接日志',{timeout:20000});
  const hasFile=await files.getByRole('log').count();
  const hasEmptyMessage=await files.locator('.experiment-log-empty').count();
  const fileConnection=await files.locator('.experiment-log-states').innerText();
  const html=await context.request.get(base+'/');
  const htmlText=await html.text();
  assert.ok(htmlText.includes(expectedAsset),'latest frontend asset loaded');
  assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
  const result={status:'passed',checked_at:new Date().toISOString(),target:title,visibleRecordCharacters,recordChannelLabel,recordStatus,
    hasFile:Boolean(hasFile),hasEmptyMessage:Boolean(hasEmptyMessage),fileConnection,responses,researchWrites:writes.length,browserErrors:errors,
    checks:['fresh context authenticates with GET session','actual T5 records visible through main log entry','same task switches back to train/eval file view','both views usable at 390px','current frontend asset confirmed','no research writes']};
  if(process.argv[2])await writeFile(process.argv[2],JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
}finally{await context.close();await browser.close();}
