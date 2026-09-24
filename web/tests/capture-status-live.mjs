import { chromium, expect } from '@playwright/test';
const base='http://127.0.0.1:8767';
const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1000},timezoneId:'Asia/Shanghai',serviceWorkers:'block'});
const writes=[], errors=[];
await context.route('**/*',route=>{
  const request=route.request();
  if(new URL(request.url()).origin!==base) return route.abort();
  if(request.method()!=='GET') { writes.push(new URL(request.url()).pathname); return route.abort(); }
  return route.continue();
});
try {
  const page=await context.newPage();
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base,{waitUntil:'domcontentloaded'});
  await expect(page.locator('.status-home-footer time')).not.toHaveText('尚无',{timeout:8000});
  await expect(page.getByRole('alert')).toHaveCount(0);
  await page.screenshot({path:new URL('./status-live-desktop.png',import.meta.url).pathname});
  const text=await page.locator('[data-testid="runtime-status"]').innerText();
  await page.setViewportSize({width:390,height:844});
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
  await page.screenshot({path:new URL('./status-live-mobile.png',import.meta.url).pathname,fullPage:true});
  console.log(JSON.stringify({status:'verified',summary:text,consoleErrors:errors,researchWrites:writes.length}));
} finally { await context.close(); await browser.close(); }
