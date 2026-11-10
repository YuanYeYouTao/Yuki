const {app, BrowserWindow} = require('electron');
const path = require('path'), fs = require('fs');
const LOG = process.env.SPIKE_LOG;
function log(o){o.t=Date.now()/1000;o.appFocused=BrowserWindow.getFocusedWindow()!==null;fs.appendFileSync(LOG, JSON.stringify(o)+'\n')}
app.on('browser-window-focus', (_, w) => log({ev:'browser-window-focus', title:w.getTitle()}));
app.on('browser-window-blur', (_, w) => log({ev:'browser-window-blur', title:w.getTitle()}));
app.whenReady().then(() => {
  for (const [i, tag] of [[0,'A'],[1,'B']]) {
    const w = new BrowserWindow({x: 1440 + i*40, y: 560 + i*40, width: 420, height: 320, show: false,
      title: 'Spike electron '+tag,
      webPreferences: {nodeIntegration: true, contextIsolation: false, backgroundThrottling: false,
        preload: path.join(__dirname, 'preload.js')}});
    w.loadFile(path.join(__dirname, '..', 'page.html'));
    w.once('ready-to-show', () => { w.showInactive(); log({ev:'shown', tag, id: w.getMediaSourceId()}); });
    w.on('page-title-updated', (e, t) => log({ev:'title', tag, title:t}));
  }
  log({ev:'ready', pid: process.pid});
});
