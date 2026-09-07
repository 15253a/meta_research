import {chromium,expect} from '@playwright/test';
import {writeFile} from 'node:fs/promises';
const output='/vepfs-mlp2/c20250511/250806010/mxm/paper_agent/meta_research_clean_20260905/workspace-recovery-fix-20260907';
const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true});
const context=await browser.newContext({viewport:{width:1700,height:1000}});
const writes=[],errors=[],requests=[],checks=[];
await context.route('**/*', route=>{
  const request=route.request();
  if(request.method()!=='GET'){writes.push({method:request.method(),url:request.url()});return route.abort();}
  return route.continue();
});
const page=await context.newPage();
page.on('pageerror',error=>errors.push(error.message));
page.on('response',response=>{if(response.url().includes('/api/'))requests.push({url:response.url(),status:response.status()});});
const shell=page.getByTestId('product-shell');
try{
  await page.goto('http://127.0.0.1:8767/?workspace=1',{waitUntil:'domcontentloaded'});
  await expect(shell).toHaveAttribute('data-shell-state','ready-active',{timeout:24000});
  await expect(page.getByTestId('current-cycle-overview')).toContainText('第 1 轮',{timeout:12000});
  await expect(page.getByTestId('workspace-partial-warning')).toBeVisible();
  checks.push('live workspace ready with Cycle 1 and partial execution warning');
  await page.screenshot({path:output+'/live-workspace.png'});
  for(const stage of ['idea','plan','bundle','reasoning']){
    await page.locator(`.spectrum-stage[data-stage="${stage}"]`).click();
    const dialog=page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText({idea:'研究思路',plan:'验证计划',bundle:'实验与证据',reasoning:'研究判断'}[stage]);
    await expect(dialog).not.toContainText('正在读取阶段结果与历史记录',{timeout:18000});
    await expect(dialog).not.toContainText('阶段结果暂不可用');
    await expect(shell).toHaveAttribute('data-shell-state','ready-active');
    await page.screenshot({path:output+`/live-${stage}.png`});
    checks.push({stage,text:(await dialog.innerText()).slice(0,1000)});
    await dialog.getByRole('button',{name:'返回研究',exact:true}).click();
    await expect(dialog).toHaveCount(0);
  }
  await page.reload({waitUntil:'domcontentloaded'});
  await expect(shell).toHaveAttribute('data-shell-state','ready-active',{timeout:24000});
  await expect(page.getByTestId('current-cycle-overview')).toContainText('第 1 轮',{timeout:12000});
  checks.push('page reload returns to ready-active');
  await page.getByRole('button',{name:'查看本轮记录与历史结果'}).click();
  await expect(page.locator('.research-history-toggle')).toHaveAttribute('aria-expanded','true');
  await expect(page.locator('main')).toContainText('研究进展');
  await expect(page.locator('.research-overview')).not.toContainText('正在读取研究发现',{timeout:18000});
  await page.screenshot({path:output+'/live-history.png'});
  checks.push({history:(await page.locator('main').innerText()).slice(-1200)});
  await page.getByRole('button',{name:'研究资料',exact:true}).click();
  await expect(shell).toHaveAttribute('data-shell-state','ready-active',{timeout:24000});
  const assets=page.getByTestId('research-assets-workbench');
  await expect(assets).toBeVisible();
  await expect(assets.locator('.asset-header-chip')).toHaveText(/^[1-9]\d* \/ [1-9]\d* versions$/,{timeout:24000});
  await page.screenshot({path:output+'/live-assets.png'});
  checks.push({assets:(await assets.innerText()).slice(0,1200)});
  const result={status:'passed',checks,requests,errors,writes};
  await writeFile(output+'/live-ui.json',JSON.stringify(result,null,2));
  console.log(JSON.stringify(result));
}catch(error){
  const result={status:'failed',error:String(error),checks,text:(await page.locator('body').innerText()).slice(0,900),requests,errors,writes};
  await writeFile(output+'/live-ui.json',JSON.stringify(result,null,2));
  console.log(JSON.stringify(result));process.exitCode=1;
}finally{await context.close();await browser.close();}
