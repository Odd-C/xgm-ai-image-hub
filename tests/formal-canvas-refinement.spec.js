const {test, expect} = require('@playwright/test');
const BASE = process.env.FORMAL_URL || `http://127.0.0.1:${process.env.E2E_PORT || 5190}`;

// 凭据来自 playwright.config.js 的 webServer.env（E2E 专用库，不碰真实数据）。
const USERNAME = process.env.E2E_USERNAME || 'admin';
const PASSWORD = process.env.E2E_PASSWORD || 'test-password';

// 用例统一从**一张空画布**出发，所以要覆盖两种登录落点：
//   1. 账号还没有项目 → GET /projects 自动建「未命名项目」并 302 进画布；
//   2. 账号已有项目 → /projects 只渲染项目列表页，得先新建一个。
// 不假设是哪种，避免依赖跨用例残留的状态（E2E 库在两次运行之间是保留的）。
async function login(page) {
  await page.goto(`${BASE}/login`);
  await page.locator('input[name="username"]').fill(USERNAME);
  await page.locator('input[name="password"]').fill(PASSWORD);
  await Promise.all([page.waitForURL(/\/projects(\/[^/]+)?$/), page.locator('button[type="submit"]').click()]);
  if (/\/projects\/[^/]+$/.test(new URL(page.url()).pathname)) return;
  await page.locator('.new-project-form input[name="name"]').fill(`e2e-${Date.now()}`);
  await Promise.all([
    page.waitForURL(/\/projects\/[^/]+$/),
    page.locator('.new-project-form button').click(),
  ]);
}
// 新建项目的入口是侧栏的 [data-new-project] → app.js 提交 #quick-create-project
// 表单 → 服务端 303 到新画布。名字由服务端生成（原对话框已移除）。
async function newProject(page) {
  const before = page.url();
  await Promise.all([
    page.waitForURL(url => url.toString() !== before),
    page.locator('#project-sidebar [data-new-project]').last().click(),
  ]);
}
async function png(page, width, height, color) {
  const base64 = await page.evaluate(({width,height,color}) => { const c=document.createElement('canvas');c.width=width;c.height=height;const x=c.getContext('2d');x.fillStyle=color;x.fillRect(0,0,width,height);x.fillStyle='#fff';x.font='32px sans-serif';x.fillText(`${width}:${height}`,20,50);return c.toDataURL('image/png').split(',')[1]; }, {width,height,color});
  return Buffer.from(base64, 'base64');
}
async function dispatchPaste(page, selector, entries) {
  return page.locator(selector).evaluate((target, entries) => {
    const transfer = new DataTransfer();
    for (const entry of entries) {
      if (entry.kind === 'file') {
        const bytes = Uint8Array.from(atob(entry.base64), char => char.charCodeAt(0));
        transfer.items.add(new File([bytes], entry.name, {type: entry.mimeType}));
      } else transfer.setData(entry.mimeType, entry.value);
    }
    return target.dispatchEvent(new ClipboardEvent('paste', {bubbles: true, cancelable: true, clipboardData: transfer}));
  }, entries);
}
async function dropTransfer(page, selector, entries, clientX, clientY) {
  return page.locator(selector).evaluate((target, {entries, clientX, clientY}) => {
    const transfer = new DataTransfer();
    for (const entry of entries) {
      if (entry.kind === 'file') {
        const bytes = Uint8Array.from(atob(entry.base64), char => char.charCodeAt(0));
        transfer.items.add(new File([bytes], entry.name, {type: entry.mimeType}));
      } else transfer.setData(entry.mimeType, entry.value);
    }
    return target.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: transfer, clientX, clientY}));
  }, {entries, clientX, clientY});
}
const fileEntry = (buffer, name, mimeType) => ({kind: 'file', base64: buffer.toString('base64'), name, mimeType});
async function deltas(page) {
  return page.evaluate(() => [...document.querySelectorAll('#canvas-links path[data-end-x]')].map(path => {
    const source=path.dataset.source,parsed=source.startsWith('result:')?source.slice(7).split(':'):null,s=parsed?document.querySelector(`[data-node-id="${parsed[0]}"] [data-result-id="${parsed[1]}"] .tile-port`):document.querySelector(`[data-node-id="${source}"] .output-port`),t=document.querySelector(`[data-node-id="${path.dataset.target}"] .input-port`),v=document.querySelector('#canvas-viewport').getBoundingClientRect(),m=new DOMMatrix(getComputedStyle(document.querySelector('#canvas-world')).transform),c=e=>{const r=e.getBoundingClientRect();return{x:r.left+r.width/2,y:r.top+r.height/2}},a=c(s),b=c(t),p=(x,y)=>({x:v.left+m.e+x*m.a,y:v.top+m.f+y*m.d}),sp=p(+path.dataset.startX,+path.dataset.startY),ep=p(+path.dataset.endX,+path.dataset.endY);return{start:Math.hypot(sp.x-a.x,sp.y-a.y),end:Math.hypot(ep.x-b.x,ep.y-b.y)};
  }));
}

test('formal external image drop paste validation and reload lifecycle', async ({page}) => {
  test.setTimeout(60000);
  const errors=[];page.on('console',m=>{if(m.type()==='error')errors.push(m.text())});page.on('pageerror',e=>errors.push(e.message));
  await page.setViewportSize({width:1440,height:960});await login(page);
  const viewport=page.locator('#canvas-viewport'),landscape=await png(page,600,200,'#496f5d'),portrait=await png(page,180,540,'#815f71');
  await page.locator('#zoom-in').click();
  const box=await viewport.boundingBox(),drop={x:box.x+420,y:box.y+310};
  const expectedDrop=await page.evaluate(({x,y})=>{const r=document.querySelector('#canvas-viewport').getBoundingClientRect(),m=new DOMMatrix(getComputedStyle(document.querySelector('#canvas-world')).transform);return{x:(x-r.left-m.e)/m.a,y:(y-r.top-m.f)/m.d}},drop);
  await dropTransfer(page,'#canvas-viewport',[fileEntry(landscape,'drop-landscape.png','image/png')],drop.x,drop.y);
  await expect(page.locator('.image-node')).toHaveCount(1);
  let positions=await page.locator('.image-node').evaluateAll(nodes=>nodes.map(n=>({x:parseFloat(n.style.left),y:parseFloat(n.style.top)})));
  expect(Math.abs(positions[0].x-expectedDrop.x)).toBeLessThanOrEqual(.001);expect(Math.abs(positions[0].y-expectedDrop.y)).toBeLessThanOrEqual(.001);

  const pointer={x:box.x+680,y:box.y+420};await page.mouse.move(pointer.x,pointer.y);await viewport.focus();
  const expectedPaste=await page.evaluate(({x,y})=>{const r=document.querySelector('#canvas-viewport').getBoundingClientRect(),m=new DOMMatrix(getComputedStyle(document.querySelector('#canvas-world')).transform);return{x:(x-r.left-m.e)/m.a,y:(y-r.top-m.f)/m.d}},pointer);
  await dispatchPaste(page,'#canvas-viewport',[fileEntry(portrait,'clipboard-portrait.png','image/png')]);
  await expect(page.locator('.image-node')).toHaveCount(2);positions=await page.locator('.image-node').evaluateAll(nodes=>nodes.map(n=>({x:parseFloat(n.style.left),y:parseFloat(n.style.top)})));
  expect(Math.abs(positions[1].x-expectedPaste.x)).toBeLessThanOrEqual(.001);expect(Math.abs(positions[1].y-expectedPaste.y)).toBeLessThanOrEqual(.001);
  const fidelity=await page.locator('.image-node img').evaluateAll(images=>images.map(image=>({ratio:image.naturalWidth/image.naturalHeight,fit:getComputedStyle(image).objectFit})));
  expect(fidelity[0].ratio).toBeCloseTo(3,4);expect(fidelity[1].ratio).toBeCloseTo(1/3,4);fidelity.forEach(item=>expect(item.fit).toBe('contain'));

  await page.locator('.image-node').first().click();await page.keyboard.press('Control+c');
  await dispatchPaste(page,'.image-node:first-child',[fileEntry(portrait,'system-image-wins.png','image/png')]);
  await expect(page.locator('.image-node')).toHaveCount(3);await expect(page.locator('.image-node').last()).toContainText('system-image-wins.png');
  await viewport.focus();await dispatchPaste(page,'#canvas-viewport',[]);await expect(page.locator('.image-node')).toHaveCount(4);

  await page.locator('#add-request').click();const prompt=page.locator('.generation-node textarea');await prompt.focus();const beforeTextPaste=await page.locator('.image-node').count();
  expect(await dispatchPaste(page,'.generation-node textarea',[{kind:'text',mimeType:'text/plain',value:'正常提示词粘贴'}])).toBeTruthy();await expect(page.locator('.image-node')).toHaveCount(beforeTextPaste);

  await viewport.focus();await dropTransfer(page,'#canvas-viewport',[fileEntry(Buffer.from('<svg/>'),'bad.svg','image/svg+xml')],drop.x,drop.y);await expect(page.locator('#toast')).toContainText('仅支持 PNG、JPEG 和 WebP');await expect(page.locator('.image-node')).toHaveCount(beforeTextPaste);
  await dispatchPaste(page,'#canvas-viewport',[fileEntry(landscape,'multi-landscape.png','image/png'),fileEntry(portrait,'multi-portrait.png','image/png')]);await expect(page.locator('.image-node')).toHaveCount(beforeTextPaste+2);
  const pair=await page.locator('.image-node').evaluateAll(nodes=>nodes.slice(-2).map(n=>({x:parseFloat(n.style.left),y:parseFloat(n.style.top),aspect:n.querySelector('.image-frame').style.aspectRatio,natural:n.querySelector('img').naturalWidth/n.querySelector('img').naturalHeight})));
  expect(pair[1].x-pair[0].x).toBe(32);expect(pair[1].y-pair[0].y).toBe(32);expect(pair[0].natural).toBeCloseTo(3,4);expect(pair[1].natural).toBeCloseTo(1/3,4);

  await expect(page.locator('.image-node.uploading')).toHaveCount(0);await expect(page.locator('#save-state')).toContainText('已保存',{timeout:5000});const persisted=await page.locator('.image-node').count();await page.reload({waitUntil:'networkidle'});await expect(page.locator('.image-node')).toHaveCount(persisted);
  /* 拖入/粘贴的本地图片现在会被上传成素材库资产（app.js:uploadImageToAsset → /api/assets），
     所以刷新后应当**直接可用**，而不是要求重新选择。旧断言「刷新后全部 .upload-missing」
     是上传功能上线之前的行为，已过期 —— 这里改成更强的断言：图片回来且 src 已是服务端地址。 */
  await expect(page.locator('.upload-missing')).toHaveCount(0);
  const restoredSources=await page.locator('.image-node img').evaluateAll(images=>images.map(image=>image.getAttribute('src')));
  expect(restoredSources.length).toBe(persisted);
  expect(restoredSources.every(src=>/^(\/assets\/|https?:)/.test(src||''))).toBe(true);
  await viewport.focus();await dispatchPaste(page,'#canvas-viewport',[{kind:'text',mimeType:'text/plain',value:'https://example.invalid/image.png'}]);await expect(page.locator('#toast')).toContainText('暂不支持仅粘贴图片链接');await expect(page.locator('.image-node')).toHaveCount(persisted);
  await dropTransfer(page,'#canvas-viewport',[{kind:'text',mimeType:'text/uri-list',value:'https://example.invalid/image.webp'}],drop.x,drop.y);await expect(page.locator('#toast')).toContainText('暂不支持仅粘贴图片链接');await expect(page.locator('.image-node')).toHaveCount(persisted);
  expect(errors).toEqual([]);
});

test('formal site canvas contract and project isolation', async ({page}) => {
  test.setTimeout(60000);
  const errors=[];page.on('console',m=>{if(m.type()==='error')errors.push(m.text())});page.on('pageerror',e=>errors.push(e.message));
  await page.setViewportSize({width:1440,height:960});await login(page);
  const landscape=await png(page,900,300,'#64796a'),portrait=await png(page,300,900,'#886b5c');
  await page.locator('#canvas-upload').setInputFiles([{name:'landscape-3x1.png',mimeType:'image/png',buffer:landscape},{name:'portrait-1x3.png',mimeType:'image/png',buffer:portrait}]);
  await expect(page.locator('.image-node')).toHaveCount(2);await page.locator('#add-request').click();await expect(page.locator('.generation-node')).toHaveCount(1);
  const image=page.locator('.image-node').first(),request=page.locator('.generation-node');const out=await image.locator('.output-port').boundingBox(),input=await request.locator('.input-port').boundingBox();await page.mouse.move(out.x+out.width/2,out.y+out.height/2);await page.mouse.down();await page.mouse.move(input.x+input.width/2,input.y+input.height/2);await page.mouse.up();await expect(page.locator('.input-link')).toHaveCount(1);
  await page.waitForTimeout(80);let values=await deltas(page);values.forEach(v=>{expect(v.start).toBeLessThanOrEqual(1.5);expect(v.end).toBeLessThanOrEqual(1.5)});
  await page.locator('#zoom-in').click();await page.waitForTimeout(80);values=await deltas(page);values.forEach(v=>{expect(v.start).toBeLessThanOrEqual(1.5);expect(v.end).toBeLessThanOrEqual(1.5)});
  await request.click();await expect(request).toHaveClass(/expanded/);const toggle=request.locator('[data-toggle-generation]'),remove=request.locator('[data-delete-node]');for(const control of [toggle,remove]){await expect(control).toHaveAttribute('aria-label',/.+/);await expect(control).toHaveAttribute('title',/.+/);await expect(control.locator('svg')).toHaveCount(1);const b=await control.boundingBox();expect(b.width).toBeGreaterThanOrEqual(28);expect(b.height).toBeGreaterThanOrEqual(28)}const coords=async()=>page.locator('.canvas-node').evaluateAll(nodes=>nodes.map(n=>[n.dataset.nodeId,n.style.left,n.style.top]));const beforeCoords=await coords(),expandedBox=await request.boundingBox(),expandedPort=await request.locator('.input-port').boundingBox();await toggle.click();await page.waitForTimeout(80);await expect(toggle).toHaveAttribute('aria-expanded','false');const collapsedBox=await request.boundingBox(),collapsedPort=await request.locator('.input-port').boundingBox();expect(collapsedBox.height).toBeLessThan(expandedBox.height);expect(collapsedPort.y).not.toBe(expandedPort.y);expect(await coords()).toEqual(beforeCoords);values=await deltas(page);values.forEach(v=>expect(v.end).toBeLessThanOrEqual(1.5));for(let i=0;i<3;i++){await toggle.click();await toggle.click()}await toggle.click();await page.waitForTimeout(80);expect(await coords()).toEqual(beforeCoords);values=await deltas(page);values.forEach(v=>{expect(v.start).toBeLessThanOrEqual(1.5);expect(v.end).toBeLessThanOrEqual(1.5)});
  const fidelity=await page.locator('.image-node img').evaluateAll(imgs=>imgs.map(i=>({fit:getComputedStyle(i).objectFit,intrinsic:i.naturalWidth/i.naturalHeight})));expect(fidelity[0].fit).toBe('contain');expect(fidelity[0].intrinsic).toBeCloseTo(3,4);expect(fidelity[1].intrinsic).toBeCloseTo(1/3,4);
  await image.click({force:true});await expect(request).toHaveClass(/expanded/);await expect(request).toHaveAttribute('aria-selected','false');const prompt=request.locator('textarea');await prompt.click();await prompt.pressSequentially('正式一次输入');await expect(prompt).toHaveValue('正式一次输入');expect(await page.evaluate(()=>document.activeElement?.tagName)).toBe('TEXTAREA');await expect(request).toHaveAttribute('aria-selected','true');await request.locator('[data-model-trigger]').click();await request.locator('[data-profile-option]:not(:disabled)').last().click();await request.locator('[data-image-settings-trigger]').click();await request.locator('[data-ratio-option]').first().click();
  /* 改模型/比例会整体重渲染卡片（selectProfile / selectImageSetting → closeActivePicker → render()），
     并把焦点**刻意**交回刚用过的选择器按钮（键盘可达性）。旧断言要求 textarea 元素本身存活
     （elementHandle.isConnected）是「同元素身份」时代的契约，已不成立：换成锁住真正要保证的两件事
     —— ① 已输入的提示词不丢；② 焦点回到刚操作的选择器。 */
  await expect(prompt).toHaveValue('正式一次输入');
  await expect(request.locator('[data-image-settings-trigger]')).toBeFocused();
  await prompt.click();await page.keyboard.type('继续');await expect(prompt).toHaveValue('正式一次输入继续');
  await page.locator('#canvas-viewport').focus();await page.keyboard.press('Control+a');await expect(page.locator('.canvas-node[aria-selected="true"]')).toHaveCount(3);await page.keyboard.press('Control+c');await page.keyboard.press('Control+v');await expect(page.locator('.canvas-node')).toHaveCount(6);await page.keyboard.press('Delete');await expect(page.locator('.canvas-node')).toHaveCount(3);
  /* 两张上传图按 32px 偏移叠放（有意为之：让人看出有两张、再自己拖开），DOM 顺序靠后的
     那张盖在上面。dblclick 走真实命中测试，点 `.first()` 的中心实际会落在**上层**那张上，
     所以「被打开的那张图」= 最上层那张，焦点也必须回到它。用 `.first()` 断言会假失败
     （实测焦点回到了上层那张，而 first 是下层那张）。 */
  const topImage=page.locator('.image-node').last();await topImage.focus();await topImage.dblclick({force:true});const viewer=page.locator('#image-viewer'),viewerImage=viewer.locator('img'),zoomLabel=viewer.locator('[data-view-zoom]');await expect(viewer).toBeVisible();await expect(viewerImage).toHaveCSS('object-fit','contain');await viewer.locator('[data-view-actual]').click();await expect(zoomLabel).toHaveText('100%');await page.keyboard.press('+');expect(parseInt(await zoomLabel.textContent())).toBeGreaterThan(100);await page.keyboard.press('-');await expect(zoomLabel).toHaveText('100%');await page.keyboard.press('0');await viewer.locator('[data-view-zoom-in]').click();expect(parseInt(await zoomLabel.textContent())).toBeGreaterThan(10);const stage=viewer.locator('.viewer-stage'),sb=await stage.boundingBox();await stage.dispatchEvent('wheel',{deltaY:-100,clientX:sb.x+sb.width*.7,clientY:sb.y+sb.height*.5});await page.keyboard.press('1');await expect(zoomLabel).toHaveText('100%');await page.keyboard.press('Escape');await expect(viewer).toBeHidden();
  await expect(topImage).toBeFocused();
  /* 平台 chips = 服务端下发的模型目录按 provider 分组的键；数量随「配置了哪些平台」
     变化（写死 3 会在增减平台时假失败）。按服务端数据推导，并要求至少一个平台，
     避免断言在空目录上真空通过。 */
  const providers=await page.evaluate(()=>[...new Set((window.IMAGE_HUB_MODELS||[]).map(item=>item.provider))]);
  expect(providers.length).toBeGreaterThan(0);
  await request.locator('[data-model-trigger]').click();await expect(request.locator('.provider-chips button')).toHaveCount(providers.length);await page.keyboard.press('Escape');
  const viewportBox=await page.locator('#canvas-viewport').boundingBox();await page.locator('#canvas-viewport').click({button:'right',position:{x:viewportBox.width-20,y:Math.min(680,viewportBox.height-20)},force:true});const menu=page.locator('#context-menu');await expect(menu).toBeVisible();const box=await menu.boundingBox();expect(box.x+box.width).toBeLessThanOrEqual(1440);expect(box.y+box.height).toBeLessThanOrEqual(960);await page.keyboard.press('Escape');
  await expect(page.locator('#minimap-map')).toHaveAttribute('data-node-count','3');const old=await page.locator('#canvas-world').evaluate(e=>getComputedStyle(e).transform);await page.locator('#minimap-map').click({position:{x:20,y:20}});expect(await page.locator('#canvas-world').evaluate(e=>getComputedStyle(e).transform)).not.toBe(old);
  const rail=image.locator('.node-action-rail');for(const action of await rail.locator('button,a').all()){await expect(action).toHaveAttribute('aria-label',/.+/);await expect(action).toHaveAttribute('title',/.+/);const b=await action.boundingBox();expect(b.width).toBeGreaterThanOrEqual(28);expect(b.height).toBeGreaterThanOrEqual(28);await expect(action.locator('svg')).toHaveCount(1)}await expect(rail.locator('a[download]')).toHaveCount(0);const foot=image.locator('footer'),f1=await foot.boundingBox();await rail.locator('[data-open-image]').hover({force:true});const f2=await foot.boundingBox();await rail.locator('[data-delete-node]').focus();const f3=await foot.boundingBox();expect({width:f2.width,height:f2.height}).toEqual({width:f1.width,height:f1.height});expect({width:f3.width,height:f3.height}).toEqual({width:f1.width,height:f1.height});
  const center=async()=>page.evaluate(()=>{const r=document.querySelector('#canvas-viewport').getBoundingClientRect(),m=new DOMMatrix(getComputedStyle(document.querySelector('#canvas-world')).transform);return{x:(r.width/2-m.e)/m.a,y:(r.height/2-m.f)/m.d,width:r.width,nodes:[...document.querySelectorAll('.canvas-node')].map(n=>[n.dataset.nodeId,n.style.left,n.style.top])}});const c1=await center();await page.locator('#sidebar-collapse').click();await page.waitForTimeout(50);const c2=await center();expect(c2.width).toBeGreaterThan(c1.width);expect(Math.abs(c2.x-c1.x)).toBeLessThan(1);expect(Math.abs(c2.y-c1.y)).toBeLessThan(1);expect(c2.nodes).toEqual(c1.nodes);values=await deltas(page);values.forEach(v=>{expect(v.start).toBeLessThanOrEqual(1.5);expect(v.end).toBeLessThanOrEqual(1.5)});
  await page.waitForTimeout(800);const firstUrl=page.url();await newProject(page);await expect(page.locator('.canvas-node')).toHaveCount(0);await page.locator('#add-request').click();await expect(page.locator('.canvas-node')).toHaveCount(1);await Promise.all([page.waitForURL(firstUrl),page.locator(`#project-sidebar a[href="${new URL(firstUrl).pathname}"]`).click()]);await expect(page.locator('.canvas-node')).toHaveCount(3);
  const resources=await page.evaluate(()=>performance.getEntriesByType('resource').map(r=>new URL(r.name).origin));expect(resources.every(x=>x===new URL(BASE).origin)).toBeTruthy();expect(errors).toEqual([]);
  for (const size of [{n:'1440x960',w:1440,h:960},{n:'1024x900',w:1024,h:900},{n:'768x900',w:768,h:900},{n:'375x812',w:375,h:812}]) { await page.setViewportSize({width:size.w,height:size.h}); if(size.w<=420) await page.reload({waitUntil:'networkidle'}); await page.waitForTimeout(80);for(const control of await page.locator('.generation-node .node-header-actions button').all()){const b=await control.boundingBox();expect(b.width).toBeGreaterThanOrEqual(size.w<=768?36:28);expect(b.height).toBeGreaterThanOrEqual(size.w<=768?36:28)} const actionGeometry=await page.locator('.generation-node .node-header-actions button').evaluateAll(nodes=>nodes.map(n=>{const r=n.getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,cy:r.y+r.height/2}})),requestBox=await page.locator('.generation-node').boundingBox();expect(Math.abs(actionGeometry[0].cy-actionGeometry[1].cy)).toBeLessThanOrEqual(.5);actionGeometry.forEach(b=>{expect(b.x).toBeGreaterThanOrEqual(requestBox.x-.75);expect(b.y).toBeGreaterThanOrEqual(requestBox.y-.75);expect(b.x+b.width).toBeLessThanOrEqual(requestBox.x+requestBox.width+.75);expect(b.y+b.height).toBeLessThanOrEqual(requestBox.y+requestBox.height+.75)});expect(actionGeometry[0].x+actionGeometry[0].width).toBeLessThanOrEqual(actionGeometry[1].x); expect(await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth)).toBeLessThanOrEqual(0); if(size.w<=768){expect((await page.locator('#canvas-viewport').boundingBox()).width).toBe(size.w);await page.locator('#mobile-projects-button').click();await expect(page.locator('#mobile-projects-button')).toHaveAttribute('aria-expanded','true');await page.keyboard.press('Escape');await expect(page.locator('#mobile-projects-button')).toHaveAttribute('aria-expanded','false');await page.locator('#mobile-projects-button').click();await page.locator('#project-scrim').click({position:{x:size.w-8,y:80}});await expect(page.locator('#mobile-projects-button')).toHaveAttribute('aria-expanded','false')} if(size.w<=420){await expect(page.locator('#minimap-toggle')).toHaveAttribute('aria-expanded','false');await page.locator('#minimap-toggle').click();await expect(page.locator('#minimap-map')).toBeVisible()}await page.screenshot({path:`qa/formal-${size.n}.png`,fullPage:true}); }
  expect(errors).toEqual([]);
});
