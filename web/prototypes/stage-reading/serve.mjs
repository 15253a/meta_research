// Isolated, read-only prototype server. Run: node prototypes/stage-reading/serve.mjs
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {dirname, resolve, extname, sep} from 'node:path';
const root = dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.STAGE_PROTOTYPE_PORT || 8777);
const mime = {'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.png':'image/png','.md':'text/plain; charset=utf-8'};
createServer(async(req,res)=>{
  if(req.method!=='GET' && req.method!=='HEAD'){res.writeHead(405);res.end();return;}
  const url = new URL(req.url,'http://127.0.0.1');
  const path = resolve(root,'.'+decodeURIComponent(url.pathname==='/'?'/index.html':url.pathname));
  if(path !== root && !path.startsWith(root+sep)){res.writeHead(403);res.end();return;}
  try {const data=await readFile(path);res.writeHead(200,{'Content-Type':mime[extname(path)]||'application/octet-stream','Cache-Control':'no-store'});res.end(req.method==='HEAD'?undefined:data);} catch {res.writeHead(404);res.end('Not found');}
}).listen(port,'127.0.0.1',()=>console.log(`Stage prototypes: http://127.0.0.1:${port}/?variant=A`));
