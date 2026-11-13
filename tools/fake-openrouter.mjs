import http from 'node:http';
import {pathToFileURL} from 'node:url';
export async function startFake(port=0){
 let scenario='valid';let requests=[];
 const server=http.createServer(async(req,res)=>{
  const json=(status,body)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(body));};
  let raw='';for await(const chunk of req){raw+=chunk;if(raw.length>30*1024*1024){json(413,{error:{message:'too large'}});return;}}
  let body={};try{body=raw?JSON.parse(raw):{};}catch{json(400,{error:{message:'JSON required by test stub'}});return;}
  if(req.url==='/_control'&&req.method==='POST'){scenario=body.scenario||'valid';requests=[];json(200,{scenario});return;}
  if(req.url==='/_requests'){json(200,{requests});return;}
  requests.push({url:req.url,model:body.model});
  if(scenario==='unauthorized'){json(401,{error:{message:'test unauthorized'}});return;}
  if(scenario==='rate_limit_once'&&requests.length===1){res.setHeader('Retry-After','1');json(429,{error:{message:'test throttling'}});return;}
  if(req.url==='/api/v1/audio/transcriptions'){
   if(scenario==='silence'){json(200,{text:'',duration:12,language:'uk',segments:[],words:[],usage:{seconds:12,cost:0}});return;}
   json(200,{text:'Спробую перестрибнути. Не вийшло!',duration:12,language:'uk',segments:[{id:0,start:1,end:3,text:'Спробую перестрибнути.'},{id:1,start:5,end:7,text:'Не вийшло!'}],words:[{word:'Спробую',start:1,end:2},{word:'перестрибнути.',start:2,end:3},{word:'Не',start:5,end:6},{word:'вийшло!',start:6,end:7}],usage:{seconds:12,cost:0}});return;
  }
  if(req.url==='/api/v1/chat/completions'){
   let input;try{input=JSON.parse(body.messages?.filter(m=>m.role==='user').at(-1)?.content||'{}');}catch{json(400,{error:{message:'User content must be structured JSON'}});return;}
   const units=input.units?.length?input.units:(scenario==='unknown_evidence'?[{id:'invalid-repair-unit'}]:[]);const category=input.profile?.categories?.[0]||'fails';
   const candidates=scenario==='empty'||!units.length?[]:[{title:'Невдала спроба',categories:[category],start_id:scenario==='unknown_evidence'?'nonexistent-unit':units[0].id,end_id:units.at(-1).id,evidence_ids:units.map(u=>u.id),reason:'Є задум і озвучений результат. Потрібно переглянути картинку.',scores:{clarity:4,hook:3,completeness:4,emotion:3,novelty:3,relevance:4},visual_review_required:true}];
   json(200,{id:'fake-completion',object:'chat.completion',choices:[{index:0,finish_reason:'stop',message:{role:'assistant',content:scenario==='malformed'?'invalid JSON':JSON.stringify({candidates})}}],usage:{prompt_tokens:100,completion_tokens:100,total_tokens:200,cost:0}});return;
  }
  json(404,{error:{message:'Unknown fake endpoint'}});
 });
 await new Promise(resolve=>server.listen(port,'127.0.0.1',resolve));
 return {base:`http://127.0.0.1:${server.address().port}`,close:()=>new Promise(resolve=>server.close(resolve))};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){const f=await startFake(Number(process.env.PORT||18766));console.log(f.base);}
