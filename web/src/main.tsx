import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Alert, Button, Card, ConfigProvider, Empty, Space, Statistic, Table, Tag, Typography, message } from 'antd';
import './style.css';

type Status = {mode:string; connected:boolean; paused:boolean; error:string; queue:number; candidate_p95_ms:number|null; samples:number};
type Candidate = {id:string; comment:{user:string;text:string}; decision:{action:string;text:string;reason:string;evidence_ids:string[]}; status:string; approved:boolean; product_id:string; created_at:number};
const labels:Record<string,string>={reply:'回复',bump:'差评顶帖',escalate:'转人工',ignore:'忽略'};

function App(){
  const [status,setStatus]=useState<Status|null>(null);
  const [rows,setRows]=useState<Candidate[]>([]);
  const [online,setOnline]=useState(false);
  const [busy,setBusy]=useState(false);
  const [msg,context]=message.useMessage();
  useEffect(()=>{
    let closed=false; let socket:WebSocket; let timer:ReturnType<typeof setTimeout>;
    function connect(){
      socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);
      socket.onopen=()=>setOnline(true);
      socket.onmessage=e=>{const d=JSON.parse(e.data);setStatus(d.status);setRows(d.candidates);};
      socket.onclose=()=>{setOnline(false);if(!closed)timer=setTimeout(connect,2000);};
    }
    connect();return ()=>{closed=true;clearTimeout(timer);socket?.close();};
  },[]);
  async function action(path:string){
    setBusy(true);
    try{const r=await fetch(`/api/${path}`,{method:'POST'});const d=await r.json();if(!r.ok)throw Error(d.detail||'操作失败');msg.success('操作已受理');}
    catch(e){msg.error(String(e));}finally{setBusy(false);}
  }
  return <ConfigProvider theme={{token:{colorPrimary:'#176b5b',borderRadius:12,fontFamily:'Inter, Microsoft YaHei, sans-serif'}}}>
    {context}<main>
    <header><div><Typography.Text type="secondary">LIVE COMMERCE / OPERATOR</Typography.Text><h1>直播间 Agent</h1><p>实时采集、知识回复与活动执行</p></div><Tag color={online?'green':'red'}>{online?'控制台已连接':'控制台连接中'}</Tag></header>
    <Space wrap><Tag color="blue">{status?.mode||'加载中'}</Tag><Tag color={status?.paused?'orange':'green'}>{status?.paused?'执行已暂停':'运行中'}</Tag><Button loading={busy} onClick={()=>action('connect')}>连接直播间</Button><Button danger onClick={()=>action('pause')}>立即暂停</Button><Button disabled={!status?.connected} onClick={()=>action('resume')}>恢复执行</Button><a href="/docs" target="_blank" rel="noreferrer">接口文档</a></Space>
    {status?.error&&<Alert type="error" showIcon message="执行已暂停" description={status.error}/>}
    {status?.mode==='shadow'&&<Alert type="info" showIcon message="影子模式：采集和生成候选，所有发送及福袋操作均被阻止。"/>}
    <section className="metrics"><Card><Statistic title="待生成事件" value={status?.queue||0}/></Card><Card><Statistic title="候选生成 P95" value={status?.candidate_p95_ms??'—'} precision={0} suffix="ms"/></Card><Card><Statistic title="延迟样本数" value={status?.samples||0}/></Card><Card><Statistic title="待人工处理" value={rows.filter(r=>r.decision.action==='escalate').length}/></Card></section>
    <Card title="评论与候选回复"><Table<Candidate> rowKey="id" dataSource={rows} scroll={{x:1050}} locale={{emptyText:<Empty description="连接直播间后，新评论会出现在这里"/>}} columns={[
      {title:'观众 / 问题',width:220,render:(_,r)=><><b>{r.comment.user}</b><p>{r.comment.text}</p></>},
      {title:'决策',width:110,render:(_,r)=><Tag>{labels[r.decision.action]||r.decision.action}</Tag>},
      {title:'建议回复 / 原因',render:(_,r)=><>{r.decision.text&&<p>{r.decision.text}</p>}<Typography.Text type="secondary">{r.decision.reason}</Typography.Text></>},
      {title:'依据 / 商品',width:180,render:(_,r)=><small>{r.decision.evidence_ids.join(', ')||'—'}<br/>{r.product_id||'未识别商品'}</small>},
      {title:'状态',dataIndex:'status',width:130},
      {title:'审核',width:130,render:(_,r)=>status?.mode==='assist'&&r.status==='pending'&&['reply','bump'].includes(r.decision.action)?<Button disabled={r.approved||busy} onClick={()=>action(`approve/${r.id}`)}>{r.approved?'已批准':'批准执行'}</Button>:'—'}
    ]}/></Card></main>
  </ConfigProvider>;
}
createRoot(document.getElementById('root')!).render(<App/>);
