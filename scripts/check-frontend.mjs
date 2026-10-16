import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync('frontend/index.html','utf8');
let count=0;
for(const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)){
 new vm.Script(match[1],{filename:`frontend-script-${++count}.js`});
}
if(!count)throw new Error('No frontend scripts found');
console.log(`${count} frontend scripts parsed successfully`);
