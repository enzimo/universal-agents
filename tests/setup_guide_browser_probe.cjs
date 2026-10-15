const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require('playwright');
const guide = pathToFileURL(path.resolve(__dirname, '../docker-compose-setup.html')).href;

// Mean brightness of the background alone. The dark background is a WebGL
// layer that cannot be read back like a 2D canvas, so this measures a
// screenshot with the page content and scrim hidden.
async function brightness(page) {
  const hidden = await page.addStyleTag({ content: 'main, header, footer, .scrim { visibility: hidden !important; }' });
  const png = await page.screenshot();
  await hidden.evaluate(element => element.remove());
  return page.evaluate(async encoded => {
    const image = new Image();
    image.src = 'data:image/png;base64,' + encoded;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext('2d');
    context.drawImage(image, 0, 0);
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let total = 0, count = 0;
    for (let i = 0; i < pixels.length; i += 400) { total += (pixels[i] + pixels[i + 1] + pixels[i + 2]) / 3; count++; }
    return total / count;
  }, png.toString('base64'));
}

(async () => {
  const browser = await chromium.launch({headless:true});
  const measurements=[];
  try {
    for(const width of [390,1280]) {
      const page = await browser.newPage({viewport:{width,height:900},colorScheme:'dark'});
      const errors=[], requests=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.route(/^https?:/,route=>{requests.push(route.request().url());return route.abort();});
      await page.goto(guide);
      for(const theme of ['dark','light','dark','light']) {
        await page.emulateMedia({colorScheme:theme});
        await page.waitForFunction(mode=>document.documentElement.dataset.colorMode===mode,theme);
        for(const delta of [0,20]) {
          await page.setViewportSize({width:width+delta,height:900});
          await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
          const mean=await brightness(page);
          assert(theme==='dark' ? mean<60 : mean>80, `${theme} background brightness ${mean}`);
          measurements.push({width:width+delta,theme,brightness:mean});
        }
      }
      for(const mode of ['desktop','runtime','hosted']) {
        await page.locator(`[data-mode="${mode}"]`).click();
        assert.equal(await page.locator('#errors').textContent(),'');
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      }
      const pane=page.locator('#github-access');
      const summary=pane.locator(':scope > summary');
      assert.equal(await pane.evaluate(el=>el.open),false);
      assert.match(await summary.textContent(),/Show steps/);
      await summary.focus();
      await page.keyboard.press('Enter');
      assert.equal(await pane.evaluate(el=>el.getAnimations().length),1);
      await pane.evaluate(el=>Promise.all(el.getAnimations().map(a=>a.finished)));
      assert.equal(await pane.evaluate(el=>el.open),true);
      assert.match(await summary.textContent(),/Hide steps/);
      await page.keyboard.press('Space');
      await pane.evaluate(el=>Promise.all(el.getAnimations().map(a=>a.finished)));
      assert.equal(await pane.evaluate(el=>el.open),false);
      await summary.evaluate(el=>{el.click();el.click();el.click();});
      await pane.evaluate(el=>Promise.all(el.getAnimations().map(a=>a.finished)));
      assert.equal(await pane.evaluate(el=>el.open),true);
      assert.equal(await pane.evaluate(el=>el.style.height),'');
      await page.emulateMedia({reducedMotion:'reduce'});
      await summary.click();
      assert.equal(await pane.evaluate(el=>el.open),false);
      assert.equal(await pane.evaluate(el=>el.getAnimations().length),0);
      await page.evaluate(()=>{location.hash='github-access';});
      await page.waitForFunction(()=>document.getElementById('github-access').open);
      const nested=pane.locator('details').first();
      await nested.locator(':scope > summary').click();
      assert.equal(await nested.evaluate(el=>el.open),true);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      assert.deepEqual(errors,[]);
      assert.deepEqual(requests,[]);
      if(process.env.UA_GUIDE_SCREENSHOTS) {
        await page.evaluate(()=>{location.hash='';scrollTo(0,0);});
        for(const theme of ['light','dark']) {
          await page.emulateMedia({colorScheme:theme});
          await page.waitForFunction(mode=>document.documentElement.dataset.colorMode===mode,theme);
          await page.screenshot({path:path.join(process.env.UA_GUIDE_SCREENSHOTS,`${theme}-${width}.png`)});
        }
        await page.locator('#xai-oauth-section > summary').click();
        await page.locator('#xai-oauth-section').scrollIntoViewIfNeeded();
        await page.screenshot({path:path.join(process.env.UA_GUIDE_SCREENSHOTS,`panes-${width}.png`)});
      }
      await page.close();
    }
    console.log(JSON.stringify({result:'pass',measurements,checks:['theme changes and resize','mobile layouts','keyboard toggles','animated height','rapid toggles','reduced motion','nested panes','deep links','no network requests']},null,2));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
