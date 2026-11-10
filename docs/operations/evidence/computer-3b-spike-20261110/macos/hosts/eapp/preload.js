const fs = require('fs');
window.spikeLog = (s) => fs.appendFileSync(process.env.SPIKE_LOG, JSON.stringify({ev:'page', data: JSON.parse(s), t: Date.now()/1000})+'\n');
