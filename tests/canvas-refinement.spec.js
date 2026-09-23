const {test, expect} = require('@playwright/test');

const DEMO_URL = process.env.DEMO_URL || 'http://127.0.0.1:4173';
const sizes = [
  {name: '1440x960', width: 1440, height: 960},
  {name: '1024x900', width: 1024, height: 900},
  {name: '768x900', width: 768, height: 900},
  {name: '375x812', width: 375, height: 812},
];

async function png(page, width, height, color) {
  const base64 = await page.evaluate(({width,height,color}) => { const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;const context=canvas.getContext('2d');context.fillStyle=color;context.fillRect(0,0,width,height);return canvas.toDataURL('image/png').split(',')[1]; }, {width,height,color});
  return Buffer.from(base64, 'base64');
}
const fileEntry = (buffer, name, mimeType) => ({kind:'file',base64:buffer.toString('base64'),name,mimeType});
async function dispatchPaste(page, selector, entries) {
  return page.locator(selector).evaluate((target, entries) => { const transfer=new DataTransfer();for(const entry of entries){if(entry.kind==='file'){const bytes=Uint8Array.from(atob(entry.base64),char=>char.charCodeAt(0));transfer.items.add(new File([bytes],entry.name,{type:entry.mimeType}))}else transfer.setData(entry.mimeType,entry.value)}return target.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:transfer})) }, entries);
}
async function dropTransfer(page, selector, entries, clientX, clientY) {
  return page.locator(selector).evaluate((target,{entries,clientX,clientY})=>{const transfer=new DataTransfer();for(const entry of entries){if(entry.kind==='file'){const bytes=Uint8Array.from(atob(entry.base64),char=>char.charCodeAt(0));transfer.items.add(new File([bytes],entry.name,{type:entry.mimeType}))}else transfer.setData(entry.mimeType,entry.value)}return target.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer,clientX,clientY}))},{entries,clientX,clientY});
}
async function endpointDeltas(page) {
  return page.evaluate(() => [...document.querySelectorAll('#links path[data-end-x]')].map(path => {
    const ref = path.dataset.source, parsed = ref.startsWith('result:') ? ref.slice(7).split(':') : null;
    const source = parsed ? document.querySelector(`[data-id="${parsed[0]}"] [data-result="${parsed[1]}"] .tile-port`) : document.querySelector(`[data-id="${ref}"] .out`);
    const target = document.querySelector(`[data-id="${path.dataset.target}"] .in`);
    const viewport = document.querySelector('#viewport').getBoundingClientRect();
    const world = document.querySelector('#world');
    const matrix = new DOMMatrix(getComputedStyle(world).transform);
    const center = element => { const r = element.getBoundingClientRect(); return {x:r.left+r.width/2, y:r.top+r.height/2}; };
    const a = center(source), b = center(target);
    const startScreen = {x: viewport.left + matrix.e + Number(path.dataset.startX) * matrix.a, y: viewport.top + matrix.f + Number(path.dataset.startY) * matrix.d};
    const endScreen = {x: viewport.left + matrix.e + Number(path.dataset.endX) * matrix.a, y: viewport.top + matrix.f + Number(path.dataset.endY) * matrix.d};
    return {start: Math.hypot(startScreen.x-a.x,startScreen.y-a.y), end: Math.hypot(endScreen.x-b.x,endScreen.y-b.y)};
  }));
}

test.beforeEach(async ({page}) => { await page.addInitScript(() => { if (!sessionStorage.getItem('demo-test-state')) { localStorage.clear(); sessionStorage.setItem('demo-test-state', '1'); } }); });

for (const size of sizes) {
  test(`responsive contract ${size.name}`, async ({page}) => {
    const errors = [];
    page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
    page.on('pageerror', error => errors.push(error.message));
    await page.setViewportSize(size);
    await page.goto(DEMO_URL, {waitUntil: 'networkidle'});
    await expect(page.locator('#minimap-map')).toHaveAttribute('data-node-count', '3');
    for(const control of await page.locator('[data-id="generation-a"] .node-header-actions button').all()){const b=await control.boundingBox();expect(b.width).toBeGreaterThanOrEqual(size.width<=768?36:28);expect(b.height).toBeGreaterThanOrEqual(size.width<=768?36:28)}
    for(const expanded of [true,false]){const nodeBox=await page.locator('[data-id="generation-a"]').boundingBox(),buttons=await page.locator('[data-id="generation-a"] .node-header-actions button').evaluateAll(nodes=>nodes.map(n=>{const r=n.getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,cy:r.y+r.height/2}}));expect(Math.abs(buttons[0].cy-buttons[1].cy)).toBeLessThanOrEqual(.5);buttons.forEach(b=>{expect(b.x).toBeGreaterThanOrEqual(nodeBox.x-.75);expect(b.y).toBeGreaterThanOrEqual(nodeBox.y-.75);expect(b.x+b.width).toBeLessThanOrEqual(nodeBox.x+nodeBox.width+.75);expect(b.y+b.height).toBeLessThanOrEqual(nodeBox.y+nodeBox.height+.75)});expect(buttons[0].x+buttons[0].width).toBeLessThanOrEqual(buttons[1].x);if(expanded)await page.locator('[data-id="generation-a"] [data-toggle]').click();}
    await expect(page.locator('#minimap-map .mini-viewport')).toHaveCount(size.width <= 420 ? 0 : 1);
    if (size.width <= 420) {
      await expect(page.locator('#minimap-toggle')).toHaveAttribute('aria-expanded', 'false');
      await page.locator('#minimap-toggle').click();
      await expect(page.locator('#minimap-map .mini-viewport')).toHaveCount(1);
    }
    if (size.width <= 768) {
      const viewport = await page.locator('#viewport').boundingBox(); expect(viewport.width).toBe(size.width);
      await page.locator('#projects-button').click(); await expect(page.locator('#project-sidebar')).toBeVisible(); await expect(page.locator('#projects-button')).toHaveAttribute('aria-expanded','true');
      await page.keyboard.press('Escape'); await expect(page.locator('#projects-button')).toHaveAttribute('aria-expanded','false');
      await page.locator('#projects-button').click(); await page.locator('#project-scrim').click({position:{x:size.width-10,y:100}}); await expect(page.locator('#projects-button')).toHaveAttribute('aria-expanded','false');
    }
    await page.locator('[data-id="landscape"]').dblclick({force:true});await expect(page.locator('#viewer')).toBeVisible();const viewerOverflow=await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth);expect(viewerOverflow).toBeLessThanOrEqual(0);for(const control of await page.locator('#viewer header button,#viewer header a').all()){const b=await control.boundingBox();expect(b.width).toBeGreaterThanOrEqual(size.width<=420?36:28);expect(b.height).toBeGreaterThanOrEqual(size.width<=420?36:28)}await page.keyboard.press('Escape');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    const resources = await page.evaluate(() => performance.getEntriesByType('resource').map(r => new URL(r.name).origin));
    expect(resources.every(origin => origin === new URL(DEMO_URL).origin)).toBeTruthy();
    expect(errors).toEqual([]);
    await page.screenshot({path: `qa/${size.name}.png`, fullPage: true});
  });
}

test('external image drop paste validation and reload lifecycle', async ({page}) => {
  const errors=[];page.on('console',message=>{if(message.type()==='error')errors.push(message.text())});page.on('pageerror',error=>errors.push(error.message));
  await page.setViewportSize({width:1440,height:960});await page.goto(DEMO_URL,{waitUntil:'networkidle'});
  const viewport=page.locator('#viewport'),landscape=await png(page,600,200,'#496f5d'),portrait=await png(page,180,540,'#815f71');await page.locator('#zoom-in').click();
  const box=await viewport.boundingBox(),drop={x:box.x+420,y:box.y+310};const expectedDrop=await page.evaluate(({x,y})=>{const r=document.querySelector('#viewport').getBoundingClientRect(),m=new DOMMatrix(getComputedStyle(document.querySelector('#world')).transform);return{x:(x-r.left-m.e)/m.a,y:(y-r.top-m.f)/m.d}},drop);
  const initial=await page.locator('article.node.image').count();await dropTransfer(page,'#viewport',[fileEntry(landscape,'drop-landscape.png','image/png')],drop.x,drop.y);await expect(page.locator('article.node.image')).toHaveCount(initial+1);
  let added=page.locator('article.node.image').nth(initial),position=await added.evaluate(node=>({x:parseFloat(node.style.left),y:parseFloat(node.style.top)}));expect(Math.abs(position.x-expectedDrop.x)).toBeLessThanOrEqual(.001);expect(Math.abs(position.y-expectedDrop.y)).toBeLessThanOrEqual(.001);
  const pointer={x:box.x+680,y:box.y+420};await page.mouse.move(pointer.x,pointer.y);await viewport.focus();const expectedPaste=await page.evaluate(({x,y})=>{const r=document.querySelector('#viewport').getBoundingClientRect(),m=new DOMMatrix(getComputedStyle(document.querySelector('#world')).transform);return{x:(x-r.left-m.e)/m.a,y:(y-r.top-m.f)/m.d}},pointer);
  await dispatchPaste(page,'#viewport',[fileEntry(portrait,'clipboard-portrait.png','image/png')]);await expect(page.locator('article.node.image')).toHaveCount(initial+2);added=page.locator('article.node.image').nth(initial+1);position=await added.evaluate(node=>({x:parseFloat(node.style.left),y:parseFloat(node.style.top)}));expect(Math.abs(position.x-expectedPaste.x)).toBeLessThanOrEqual(.001);expect(Math.abs(position.y-expectedPaste.y)).toBeLessThanOrEqual(.001);
  const fidelity=await page.locator('article.node.image').evaluateAll(nodes=>nodes.slice(-2).map(node=>{const image=node.querySelector('img');return{ratio:image.naturalWidth/image.naturalHeight,fit:getComputedStyle(image).objectFit}}));expect(fidelity[0].ratio).toBeCloseTo(3,4);expect(fidelity[1].ratio).toBeCloseTo(1/3,4);fidelity.forEach(item=>expect(item.fit).toBe('contain'));
  await page.locator('article.node.image').nth(initial).click();await page.keyboard.press('Control+c');await dispatchPaste(page,'[data-id="landscape"]',[fileEntry(portrait,'system-image-wins.png','image/png')]);await expect(page.locator('article.node.image')).toHaveCount(initial+3);await viewport.focus();await dispatchPaste(page,'#viewport',[]);await expect(page.locator('article.node.image')).toHaveCount(initial+4);
  const textarea=page.locator('[data-id="generation-a"] textarea');await textarea.focus();const before=await page.locator('article.node.image').count();expect(await dispatchPaste(page,'[data-id="generation-a"] textarea',[{kind:'text',mimeType:'text/plain',value:'正常提示词粘贴'}])).toBeTruthy();await expect(page.locator('article.node.image')).toHaveCount(before);
  await viewport.focus();await dropTransfer(page,'#viewport',[fileEntry(Buffer.from('<svg/>'),'bad.svg','image/svg+xml')],drop.x,drop.y);await expect(page.locator('#toast')).toContainText('仅支持 PNG、JPEG 和 WebP');await expect(page.locator('article.node.image')).toHaveCount(before);
  await dispatchPaste(page,'#viewport',[fileEntry(landscape,'multi-landscape.png','image/png'),fileEntry(portrait,'multi-portrait.png','image/png')]);await expect(page.locator('article.node.image')).toHaveCount(before+2);const pair=await page.locator('article.node.image').evaluateAll(nodes=>nodes.slice(-2).map(node=>({x:parseFloat(node.style.left),y:parseFloat(node.style.top),ratio:node.querySelector('img').naturalWidth/node.querySelector('img').naturalHeight})));expect(pair[1].x-pair[0].x).toBe(32);expect(pair[1].y-pair[0].y).toBe(32);expect(pair[0].ratio).toBeCloseTo(3,4);expect(pair[1].ratio).toBeCloseTo(1/3,4);
  const persisted=await page.locator('article.node.image').count();await page.reload({waitUntil:'networkidle'});await expect(page.locator('article.node.image')).toHaveCount(persisted);await expect(page.locator('.upload-missing')).toHaveCount(persisted-2);
  await viewport.focus();await dispatchPaste(page,'#viewport',[{kind:'text',mimeType:'text/plain',value:'https://example.invalid/image.png'}]);await expect(page.locator('#toast')).toContainText('暂不支持仅粘贴图片链接');await expect(page.locator('article.node.image')).toHaveCount(persisted);await dropTransfer(page,'#viewport',[{kind:'text',mimeType:'text/uri-list',value:'https://example.invalid/image.webp'}],drop.x,drop.y);await expect(page.locator('#toast')).toContainText('暂不支持仅粘贴图片链接');await expect(page.locator('article.node.image')).toHaveCount(persisted);expect(errors).toEqual([]);
});

test('geometry selection shortcuts picker context viewer and isolation', async ({page}) => {
  await page.setViewportSize({width: 1440, height: 960});
  await page.goto(DEMO_URL, {waitUntil: 'networkidle'});
  await page.locator('#zoom').click();
  let deltas = await endpointDeltas(page);
  expect(deltas.length).toBe(2);
  deltas.forEach(delta => { expect(delta.start).toBeLessThanOrEqual(1.5); expect(delta.end).toBeLessThanOrEqual(1.5); });

  const image = page.locator('[data-id="landscape"]');
  const request = page.locator('[data-id="generation-a"]');
  await image.click();
  await request.locator('header').click({modifiers:['Shift']});
  await expect(image).toHaveAttribute('aria-selected', 'true');
  await expect(request).toHaveAttribute('aria-selected', 'true');
  const before = await page.evaluate(() => ['landscape','generation-a'].map(id => { const n=document.querySelector(`[data-id="${id}"]`); return [parseFloat(n.style.left),parseFloat(n.style.top)]; }));
  const box = await image.boundingBox();
  await page.mouse.move(box.x+80,box.y+40); await page.mouse.down(); await page.mouse.move(box.x+130,box.y+80); await page.mouse.up();
  const after = await page.evaluate(() => ['landscape','generation-a'].map(id => { const n=document.querySelector(`[data-id="${id}"]`); return [parseFloat(n.style.left),parseFloat(n.style.top)]; }));
  expect(after[0][0]-before[0][0]).toBeCloseTo(after[1][0]-before[1][0], 1);
  expect(await page.locator('.node').count()).toBe(3);
  deltas = await endpointDeltas(page); deltas.forEach(delta => { expect(delta.start).toBeLessThanOrEqual(1.5); expect(delta.end).toBeLessThanOrEqual(1.5); });


  if (await page.locator('#viewer').evaluate(el => el.open)) await page.keyboard.press('Escape');
  await page.locator('#zoom-in').click();
  deltas = await endpointDeltas(page); deltas.forEach(delta => { expect(delta.start).toBeLessThanOrEqual(1.5); expect(delta.end).toBeLessThanOrEqual(1.5); });
  const toggle=request.locator('[data-toggle]'), remove=request.locator('[data-delete]');
  for(const control of [toggle,remove]){await expect(control).toHaveAttribute('aria-label',/.+/);await expect(control).toHaveAttribute('title',/.+/);await expect(control.locator('svg')).toHaveCount(1);const b=await control.boundingBox();expect(b.width).toBeGreaterThanOrEqual(28);expect(b.height).toBeGreaterThanOrEqual(28)}
  const nodeCoordinates=async()=>page.evaluate(()=>JSON.parse(localStorage.getItem('image-hub-demo-v5')).projects.find(p=>p.id==='spring').nodes.map(n=>[n.id,n.x,n.y]));
  const coordinatesBefore=await nodeCoordinates(), expandedBox=await request.boundingBox(), expandedPort=await request.locator('.in').boundingBox();
  await toggle.click();await page.waitForTimeout(80);await expect(toggle).toHaveAttribute('aria-expanded','false');const collapsedBox=await request.boundingBox(),collapsedPort=await request.locator('.in').boundingBox();expect(collapsedBox.height).toBeLessThan(expandedBox.height);expect(collapsedPort.y).not.toBe(expandedPort.y);expect(await nodeCoordinates()).toEqual(coordinatesBefore);deltas=await endpointDeltas(page);deltas.forEach(delta=>expect(delta.end).toBeLessThanOrEqual(1.5));
  for(let i=0;i<3;i++){await toggle.click();await toggle.click()}await toggle.click();await page.waitForTimeout(80);expect(await nodeCoordinates()).toEqual(coordinatesBefore);
  deltas = await endpointDeltas(page); deltas.forEach(delta => { expect(delta.start).toBeLessThanOrEqual(1.5); expect(delta.end).toBeLessThanOrEqual(1.5); });

  const fit = await page.locator('[data-id="landscape"] .frame').evaluate(el => { const img=el.querySelector('img'),r=img.getBoundingClientRect();return{fit:getComputedStyle(img).objectFit,ratio:r.width/r.height,intrinsic:img.naturalWidth/img.naturalHeight}; });
  expect(fit.fit).toBe('contain'); expect(fit.intrinsic).toBeCloseTo(3, 4); expect(fit.ratio).toBeCloseTo(3, 2);
  const portrait = await page.locator('[data-id="generation-a"] .result-tile img').evaluate(img => ({fit:getComputedStyle(img).objectFit,intrinsic:img.naturalWidth/img.naturalHeight}));
  expect(portrait.fit).toBe('contain'); expect(portrait.intrinsic).toBeCloseTo(1/3, 4);

  await request.locator('[data-model-trigger]').click();
  await expect(request.locator('.provider-chips button')).toHaveCount(3);
  await expect(request.locator('[data-profile="libtv:legacy"]')).toBeDisabled();
  await request.locator('[data-profile="lovart:nano"]').click();
  await expect(request.locator('[data-image-trigger] span')).toHaveText('3:4 · 2K');

  const viewportBox=await page.locator('#viewport').boundingBox();await page.locator('#viewport').click({button:'right',position:{x:viewportBox.width-20,y:Math.min(680,viewportBox.height-20)}, force:true});
  const menu = page.locator('#context-menu'); await expect(menu).toBeVisible();
  const menuBox = await menu.boundingBox(); expect(menuBox.x+menuBox.width).toBeLessThanOrEqual(1440); expect(menuBox.y+menuBox.height).toBeLessThanOrEqual(960);
  await page.keyboard.press('Escape'); await expect(menu).toBeHidden();
  await page.keyboard.press('?'); await expect(page.locator('#shortcuts')).toBeVisible(); await page.keyboard.press('Escape');

  await image.focus();await image.dblclick();const viewer=page.locator('#viewer'),viewerImage=viewer.locator('img'),zoomLabel=page.locator('#viewer-zoom');await expect(viewer).toBeVisible();await expect(viewerImage).toHaveCSS('object-fit','contain');await expect(zoomLabel).not.toHaveText('100%');const fitBox=await viewerImage.boundingBox(),stageBox=await viewer.locator('.viewer-stage').boundingBox();expect(fitBox.width).toBeLessThanOrEqual(stageBox.width+1);expect(fitBox.height).toBeLessThanOrEqual(stageBox.height+1);await page.locator('#viewer-actual').click();await expect(zoomLabel).toHaveText('100%');await page.keyboard.press('+');expect(parseInt(await zoomLabel.textContent())).toBeGreaterThan(100);await page.keyboard.press('-');await expect(zoomLabel).toHaveText('100%');await page.keyboard.press('0');await expect(zoomLabel).not.toHaveText('100%');await viewer.locator('.viewer-stage').dispatchEvent('wheel',{deltaY:-100,clientX:stageBox.x+stageBox.width*.7,clientY:stageBox.y+stageBox.height*.5});const zoomed=parseInt(await zoomLabel.textContent());expect(zoomed).toBeGreaterThan(parseInt((await zoomLabel.textContent())||'0')/1.13);await page.keyboard.press('1');await expect(zoomLabel).toHaveText('100%');await page.keyboard.press('Escape');await expect(viewer).toBeHidden();await expect(image).toBeFocused();
  await page.locator('[data-id="generation-a"]').click(); await expect(page.locator('[data-id="generation-a"] .generation-editor textarea')).toHaveValue(/保留产品比例/);

  const oldView = await page.evaluate(() => getComputedStyle(document.querySelector('#world')).transform);
  await page.locator('#minimap-map').click({position:{x:20,y:20}});
  const newView = await page.evaluate(() => getComputedStyle(document.querySelector('#world')).transform);
  expect(newView).not.toBe(oldView);

  await page.locator('#project-sidebar [data-new]').first().click(); await page.locator('#new-project-name').fill('隔离画布乙'); await page.locator('#create-project').click();
  await expect(page.locator('.node')).toHaveCount(0); await page.locator('#add-request').click(); await expect(page.locator('.node')).toHaveCount(1);
  await page.locator('[data-project="spring"]').click(); await expect(page.locator('.node')).toHaveCount(2);
});

test('direct prompt editing action rail and sidebar geometry', async ({page}) => {
  await page.setViewportSize({width:1440,height:960});
  await page.goto(DEMO_URL,{waitUntil:'networkidle'});
  const request=page.locator('[data-id="generation-a"]'), textarea=request.locator('textarea');
  await page.locator('[data-id="generation-a"]').click();
  await expect(request).toHaveClass(/expanded/); await expect(request).toHaveAttribute('aria-selected','false');
  const handle=await textarea.elementHandle(); await textarea.click(); await textarea.pressSequentially('｜一次输入');
  await expect(textarea).toHaveValue('保留产品比例，使用自然侧光与克制的浅灰背景｜一次输入');
  expect(await page.evaluate(()=>document.activeElement?.tagName)).toBe('TEXTAREA'); await expect(request).toHaveAttribute('aria-selected','true');
  await request.locator('[data-model-trigger]').click(); await request.locator('[data-profile="lovart:nano"]').click();
  await request.locator('[data-image-trigger]').click(); await request.locator('[data-ratio="3:4"]').click();
  expect(await handle.evaluate((el)=>el.isConnected)).toBeTruthy(); await handle.focus(); await page.keyboard.type('｜继续');
  await expect(textarea).toHaveValue('保留产品比例，使用自然侧光与克制的浅灰背景｜一次输入｜继续');

  const result=page.locator('[data-id="generation-a"]'), footer=result.locator('footer'), rail=result.locator('.image-actions');
  for(const action of await rail.locator('a,button').all()){await expect(action).toHaveAttribute('aria-label',/.+/);await expect(action).toHaveAttribute('title',/.+/);const box=await action.boundingBox();expect(box.width).toBeGreaterThanOrEqual(28);expect(box.height).toBeGreaterThanOrEqual(28);await expect(action.locator('svg')).toHaveCount(1)}
  const before=await footer.boundingBox(); await rail.locator('[data-open-result]').hover(); const hover=await footer.boundingBox(); await rail.locator('[data-delete]').focus(); const focused=await footer.boundingBox(); expect({width:hover.width,height:hover.height}).toEqual({width:before.width,height:before.height});expect({width:focused.width,height:focused.height}).toEqual({width:before.width,height:before.height});
  const overlap=await page.evaluate(()=>{const m=document.querySelector('[data-id="generation-a"] footer>span').getBoundingClientRect(),a=document.querySelector('[data-id="generation-a"] .image-actions').getBoundingClientRect();return m.right>a.left+.5});expect(overlap).toBeFalsy();
  await expect(rail.locator('[data-download-result]')).toBeEnabled();
  await rail.locator('[data-open-result]').click(); await expect(page.locator('#viewer')).toBeVisible(); await expect(page.locator('#viewer-original')).toHaveAttribute('href',/demo-portrait\.svg$/); await page.keyboard.press('Escape');

  const center=async()=>page.evaluate(()=>{const r=document.querySelector('#viewport').getBoundingClientRect(),s=JSON.parse(localStorage.getItem('image-hub-demo-v5')).projects.find(p=>p.id==='spring');return{x:(r.width/2-s.view.x)/s.view.z,y:(r.height/2-s.view.y)/s.view.z,width:r.width,nodes:s.nodes.map(n=>[n.id,n.x,n.y])}});
  const c1=await center(); await page.locator('#sidebar-collapse').click(); await page.waitForTimeout(50); const c2=await center(); expect(c2.width).toBeGreaterThan(c1.width); expect(Math.abs(c2.x-c1.x)).toBeLessThan(1); expect(Math.abs(c2.y-c1.y)).toBeLessThan(1); expect(c2.nodes).toEqual(c1.nodes); const ds=await endpointDeltas(page);ds.forEach(d=>{expect(d.start).toBeLessThanOrEqual(1.5);expect(d.end).toBeLessThanOrEqual(1.5)});
});
