
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeUrl=u=>/^https?:\/\//i.test(u)?u:"#";
let files=[],data=null,filter="all";

function tab(n){[1,2,3].forEach(i=>{$("p"+i).hidden=i!==n;$("t"+i).setAttribute("aria-selected",i===n)})}
$("t1").onclick=()=>tab(1);$("t2").onclick=()=>tab(2);$("t3").onclick=()=>tab(3);

function setFiles(list){files=[...list].slice(0,3);$("flist").innerHTML=files.map(f=>`<span>${esc(f.name)}</span>`).join("")}
$("drop").onclick=()=>$("file").click();
$("drop").onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();$("file").click()}};
$("file").onchange=e=>setFiles(e.target.files);
["dragover","dragenter"].forEach(ev=>$("drop").addEventListener(ev,e=>{e.preventDefault();$("drop").classList.add("over")}));
["dragleave","drop"].forEach(ev=>$("drop").addEventListener(ev,e=>{e.preventDefault();$("drop").classList.remove("over")}));
$("drop").addEventListener("drop",e=>setFiles(e.dataTransfer.files));

function say(id,t,cls){const m=$(id);m.textContent=t;m.className="msg "+(cls||"")}

async function responseJson(r){
  try{return await r.json()}catch(e){
    throw new Error(r.status===413?"This upload exceeds the file size limit.":"BibGuard could not read the server response. Please try again.");
  }
}

async function waitForCheck(id){
  for(let attempt=0;attempt<600;attempt++){
    await new Promise(resolve=>setTimeout(resolve,1500));
    let response;
    try{response=await fetch("/api/verify-jobs/"+encodeURIComponent(id))}
    catch(e){say("m1","Connection interrupted. Reconnecting to the running check...");continue}
    const job=await responseJson(response);
    if(!response.ok)throw new Error(job.error||"The check could not be completed.");
    if(job.status==="complete")return job;
    say("m1",`Checking references… ${job.done} of ${job.total} completed.`);
  }
  throw new Error("The check is taking longer than expected. Try a smaller batch.");
}

$("run").onclick=async()=>{
  const fd=new FormData();files.forEach(f=>fd.append("files",f));fd.append("text",$("text").value);
  fd.append("background","1");
  if(!files.length&&!$("text").value.trim())return say("m1","Add a file or paste references first.","err");
  $("run").disabled=true;prog(true);say("m1","Preparing your reference check…");$("out").innerHTML="";
  try{
    const r=await fetch("/api/verify",{method:"POST",body:fd}),j=await responseJson(r);
    if(!r.ok){if(r.status===402||j.pay)openPay(j.error);throw new Error(j.error||"Something went wrong")}
    const result=r.status===202?await waitForCheck(j.job):j;
    data=result.files;filter="all";render();
    say("m1",`Done. File checks left${result.beta?" today":""}: ${result.left}.`,"good");
    me();
  }catch(e){say("m1",e.message,"err")}
  finally{$("run").disabled=false;prog(false)}
};

function paperLink(m){
  if(!m.link)return "";
  const label=m.doi?"Open publisher page":"Open source record";
  return ` <a href="${esc(safeUrl(m.link))}" target="_blank" rel="noopener noreferrer">${label}</a>`;
}

function render(){
  $("out").innerHTML=data.map((f,fi)=>{
    const shown=f.entries.filter(e=>filter==="all"||(filter==="ret"?e.retracted:e.status===filter));
    const chip=(k,l)=>`<button class="chip" data-f="${k}" aria-pressed="${filter===k}">${l}</button>`;
    return `<div class="card"><h2>${esc(f.name)}</h2>
    <div class="mute">${f.total} references checked${f.note?" - "+esc(f.note):""}. ${f.corrected_count} updated or generated · ${f.review_count} need manual review.</div>
    <div class="chips">${chip("all","All "+f.total)}${chip("verified","Verified "+f.verified)}${chip("partial","Check details "+f.partial)}${chip("found","Record found "+f.found)}${chip("not_found","Not found "+f.not_found)}${chip("unavailable","Lookup unavailable "+f.unavailable)}${chip("ret","Retracted "+f.retracted)}
    <button class="chip" data-dl="${fi}">Download corrected BibTeX (.bib)</button><button class="chip" data-csv="${fi}">Download report (CSV)</button>
    <select data-sel="${fi}" aria-label="Style for copy all">${Object.entries(STYLE_NAMES).map(([k,v])=>`<option value="${k}">${v}</option>`).join("")}</select><button class="chip" data-all="${fi}">Copy all verified</button><span class="mute" data-note="${fi}" role="status"></span></div>
    ${shown.map(e=>`<div class="ref ${e.status}">
      <span class="tag ${e.status}">${{verified:"Verified",partial:"Check details",found:"Record found",not_found:"Not found",unavailable:"Lookup unavailable"}[e.status]}</span>${e.retracted?'<span class="tag ret">RETRACTED</span>':""}
      <span class="key">${esc(e.key)}</span><b>${esc(e.title)}</b>
      ${e.issues.length?`<ul class="issues">${e.issues.map(i=>`<li>${esc(i)}</li>`).join("")}</ul>`:""}
      ${e.match?`<div class="match">Real record (${esc(e.match.src)}): ${esc(e.match.authors)} (${esc(e.match.year)}). ${esc(e.match.title)}. ${esc(e.match.venue)}
      ${e.corrected_status==="verified"?" · Export matches this record":""}${e.retraction_check?" · "+esc(e.retraction_check):""}${paperLink(e.match)}${scopusTag(e.match)}${citeHtml(e.match)}</div>`:""}
    </div>`).join("")||'<p class="mute">No references in this group.</p>'}</div>`}).join("");
  document.querySelectorAll("[data-f]").forEach(b=>b.onclick=()=>{filter=b.dataset.f;render()});
  bindCsv();
  document.querySelectorAll("[data-all]").forEach(b=>b.onclick=()=>bulk(+b.dataset.all));
  document.querySelectorAll("[data-dl]").forEach(b=>b.onclick=()=>{
    const f=data[b.dataset.dl],blob=new Blob([f.corrected_bib],{type:"text/plain"});
    const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="corrected_"+f.name.replace(/\.\w+$/,"")+".bib";a.click();
  });
}

function bindCsv(){document.querySelectorAll("[data-csv]").forEach(b=>b.onclick=()=>{
    const f=data[b.dataset.csv],q=v=>{v=String(v??"");if(/^[=+\-@\t\r]/.test(v))v="'"+v;return '"'+v.replace(/"/g,'""')+'"'};
    const rows=[["key","title","status","retracted","issues","real_title","real_authors","year","venue","doi","link","source","source_errors","retraction_check","updated_fields","export_metadata_status"]].concat(f.entries.map(e=>
      [e.key,e.title,e.status,e.retracted?"yes":"no",e.issues.join(" | "),e.match?.title,e.match?.authors,e.match?.year,e.match?.venue,e.match?.doi,e.match?.link,e.match?.src,e.lookup_details?.join("; "),e.retraction_check,e.corrected_fields?.join(", "),e.corrected_status]));
    const a=document.createElement("a");a.href=URL.createObjectURL(new Blob(["\ufeff"+rows.map(r=>r.map(q).join(",")).join("\n")],{type:"text/csv"}));
    a.download="report_"+f.name.replace(/\.\w+$/,"")+".csv";a.click();});}
$("run2").onclick=async()=>{
  if(!$("fa").files[0]||!$("fb").files[0])return say("m2","Choose both files.","err");
  const fd=new FormData();fd.append("original",$("fa").files[0]);fd.append("modified",$("fb").files[0]);
  prog(true);say("m2","Comparing...");
  try{
    const r=await fetch("/api/diff",{method:"POST",body:fd}),j=await r.json();
    if(!r.ok)throw new Error(j.error);
    say("m2",`${j.checked} entries compared. ${j.changed} entries were changed.`,j.changed?"err":"good");
    $("diffout").innerHTML=j.changes.length?`<table><tr><th>Key<th>Field<th>Original<th>After AI</tr>${j.changes.map(c=>`<tr><td>${esc(c.key)}<td>${esc(c.field)}<td><del>${esc(c.old)}</del><td><ins>${esc(c.new)}</ins></tr>`).join("")}</table>`:"";
  }catch(e){say("m2",e.message,"err")}
  prog(false);
};

/*FMT*/
const ini=g=>(g||"").split(/[\s.]+/).filter(Boolean).map(p=>p.split("-").map(x=>x[0].toUpperCase()+".").join("-")).join(" ");
const AU=m=>(m.authors_list||[]).filter(a=>a[0]);
const pg=(p,sep="\u2013")=>(p||"").replace(/\s*[-\u2013\u2014]+\s*/g,sep);
const dot=s=>/[.?!]$/.test(s)?s:s+".";
const isProc=m=>/proceed|conference|symposium|workshop/i.test((m.type||"")+" "+(m.venue||""));
const bv=s=>"{"+String(s).replace(/&/g,"\\&")+"}";
const STYLES={
ieee:m=>{const a=AU(m).map(x=>(ini(x[1])+" "+x[0]).trim());const au=a.length>6?a[0]+" et al.":a.length>2?a.slice(0,-1).join(", ")+", and "+a[a.length-1]:a.join(" and ");
 const p=[];if(m.volume)p.push("vol. "+m.volume);if(m.issue)p.push("no. "+m.issue);if(m.pages)p.push("pp. "+pg(m.pages));if(m.year)p.push(m.year);
 return (au?au+", ":"")+'"'+m.title+',"'+(m.venue?" "+m.venue+",":"")+" "+p.join(", ")+(m.doi?", doi: "+m.doi:"")+"."},
apa:m=>{const a=AU(m).map(x=>x[0]+(x[1]?", "+ini(x[1]):""));let au;
 if(a.length>20)au=a.slice(0,19).join(", ")+", ... "+a[a.length-1];else au=a.length>1?a.slice(0,-1).join(", ")+(a.length>2?",":"")+" & "+a[a.length-1]:a.join("");
 let s=(au?dot(au)+" ":"")+"("+(m.year||"n.d.")+"). "+dot(m.title);
 if(m.venue){s+=" "+m.venue;if(m.volume)s+=", "+m.volume+(m.issue?"("+m.issue+")":"");if(m.pages)s+=", "+pg(m.pages);s+="."}
 return s+(m.doi?" https://doi.org/"+m.doi:"")},
harvard:m=>{const a=AU(m).map(x=>x[0]+(x[1]?", "+ini(x[1]).replace(/ /g,""):""));
 const au=a.length>3?a[0]+" et al.":a.length>1?a.slice(0,-1).join(", ")+" and "+a[a.length-1]:a.join("");
 let s=(au?au+" ":"")+"("+(m.year||"n.d.")+") '"+m.title+"'";
 if(m.venue){s+=", "+m.venue;if(m.volume)s+=", "+m.volume+(m.issue?"("+m.issue+")":"");if(m.pages)s+=", pp. "+pg(m.pages)}
 return s+"."+(m.doi?" doi: "+m.doi+".":"")},
mla:m=>{const a=AU(m);let au="";
 if(a.length===1)au=a[0][0]+", "+a[0][1];else if(a.length===2)au=a[0][0]+", "+a[0][1]+", and "+a[1][1]+" "+a[1][0];else if(a.length>2)au=a[0][0]+", "+a[0][1]+", et al";
 au=au.replace(/, $/,"");const p=[];if(m.venue)p.push(m.venue);if(m.volume)p.push("vol. "+m.volume);if(m.issue)p.push("no. "+m.issue);if(m.year)p.push(m.year);if(m.pages)p.push("pp. "+pg(m.pages));
 return (au?dot(au)+" ":"")+'"'+dot(m.title)+'" '+p.join(", ")+"."+(m.doi?" https://doi.org/"+m.doi+".":"")},
chicago:m=>{const a=AU(m).slice(0,m.authors_list.length>10?7:10);const l=a.map((x,i)=>i===0?x[0]+(x[1]?", "+x[1]:""):(x[1]?x[1]+" ":"")+x[0]);
 let au=l.length>2?l.slice(0,-1).join(", ")+", and "+l[l.length-1]:l.join(" and ");if(AU(m).length>10)au=l.join(", ")+", et al.";
 let s=(au?dot(au)+" ":"")+(m.year||"n.d.")+'. "'+dot(m.title)+'" '+(m.venue||"")+(m.volume?" "+m.volume:"")+(m.issue?" ("+m.issue+")":"")+(m.pages?": "+pg(m.pages):"");
 return s.trim().replace(/\s+/g," ")+"."+(m.doi?" https://doi.org/"+m.doi+".":"")},
vancouver:m=>{const a=AU(m).map(x=>x[0]+(x[1]?" "+ini(x[1]).replace(/[. ]/g,""):""));const au=a.length>6?a.slice(0,6).join(", ")+", et al":a.join(", ");
 return (au?au+". ":"")+dot(m.title)+(m.venue?" "+m.venue+".":"")+" "+m.year+(m.volume?";"+m.volume+(m.issue?"("+m.issue+")":""):"")+(m.pages?":"+pg(m.pages,"-"):"")+"."+(m.doi?" doi:"+m.doi:"")},
bibtex:m=>{const a=AU(m);const k=((a[0]?a[0][0]:"ref").normalize("NFD").replace(/[^A-Za-z]/g,"").toLowerCase()||"ref")+(m.year||"")+((m.title.match(/[A-Za-z]{4,}/)||["x"])[0].toLowerCase());
 const pr=isProc(m);const f=[["title",bv(m.title)],["author",bv(a.map(x=>x[1]?x[0]+", "+x[1]:x[0]).join(" and "))],[pr?"booktitle":"journal",m.venue&&bv(m.venue)],["year",m.year&&"{"+m.year+"}"],["volume",m.volume&&"{"+m.volume+"}"],["number",m.issue&&"{"+m.issue+"}"],["pages",m.pages&&"{"+pg(m.pages,"--")+"}"],["doi",m.doi&&"{"+m.doi+"}"]].filter(x=>x[1]);
 return "@"+(pr?"inproceedings":"article")+"{"+k+",\n"+f.map(x=>"  "+x[0]+" = "+x[1]).join(",\n")+"\n}"},
ris:m=>{const pr=isProc(m),L=["TY  - "+(pr?"CPAPER":"JOUR")];AU(m).forEach(x=>L.push("AU  - "+x[0]+(x[1]?", "+x[1]:"")));L.push("TI  - "+m.title);
 if(m.venue)L.push((pr?"T2":"JO")+"  - "+m.venue);if(m.year)L.push("PY  - "+m.year);if(m.volume)L.push("VL  - "+m.volume);if(m.issue)L.push("IS  - "+m.issue);
 if(m.pages){const q=m.pages.split(/[-\u2013]/);L.push("SP  - "+q[0]);if(q[1])L.push("EP  - "+q[1])}if(m.doi){L.push("DO  - "+m.doi);L.push("UR  - https://doi.org/"+m.doi)}L.push("ER  - ");return L.join("\n")}
};
const STYLE_NAMES={ieee:"IEEE",apa:"APA 7",harvard:"Harvard",mla:"MLA 9",chicago:"Chicago (author-date)",vancouver:"Vancouver",bibtex:"BibTeX",ris:"RIS (EndNote, Zotero, Mendeley)"};
const scopusTag=m=>m.scopus===true?'<span class="tag verified">Indexed in Scopus</span>':m.scopus===false?'<span class="tag partial">Not found in Scopus</span>':"";
function uniqKeys(list){const seen={};return list.map(t=>t.replace(/^(@\w+\{)([^,]+),/,(_,a,k)=>{seen[k]=(seen[k]||0)+1;return a+k+(seen[k]>1?String.fromCharCode(96+seen[k]):"")+","}))}
function bulkText(ms,sel){
  let list=ms.map(m=>STYLES[sel](m));
  if(["apa","harvard","mla","chicago"].includes(sel))list.sort((a,b)=>a.localeCompare(b));
  if(sel==="bibtex")list=uniqKeys(list);
  if(sel==="ieee")list=list.map((t,i)=>`[${i+1}] ${t}`);
  if(sel==="vancouver")list=list.map((t,i)=>`${i+1}. ${t}`);
  return list.join("\n\n");
}
/*ENDFMT*/
const MS={};let mid=0;
function citeHtml(m){MS[++mid]=m;return `<details class="cite" data-m="${mid}"><summary>Citation styles and copy</summary><div class="cbody"></div></details>`}
async function copyText(t){try{await navigator.clipboard.writeText(t);return true}catch(e){const x=document.createElement("textarea");x.value=t;document.body.appendChild(x);x.select();let ok=false;try{ok=document.execCommand("copy")}catch(_){}x.remove();return ok}}
function buildCite(d){
  const m=MS[d.dataset.m],b=d.querySelector(".cbody");
  b.innerHTML=`<select aria-label="Citation style">${Object.entries(STYLE_NAMES).map(([k,v])=>`<option value="${k}">${v}</option>`).join("")}<option value="csl">Other style (any CSL name)</option></select>
  <span class="cslbox" hidden><input list="cslnames" placeholder="e.g. nature" aria-label="CSL style name"><button class="chip get" type="button">Get</button></span>
  <button class="chip copy" type="button">Copy</button><span class="mute note"></span><div class="ctext" aria-live="polite"></div>`;
  const sel=b.querySelector("select"),out=b.querySelector(".ctext"),note=b.querySelector(".note"),box=b.querySelector(".cslbox");
  const show=()=>{note.textContent="";box.hidden=sel.value!=="csl";out.textContent=sel.value==="csl"?"Type a CSL style name (Nature, ACM, Elsevier, ACS, AMA, Springer...) and press Get.":STYLES[sel.value](m)};
  sel.onchange=show;show();
  b.querySelector(".get").onclick=async()=>{
    const st=box.querySelector("input").value.trim().toLowerCase();if(!m.doi){out.textContent="Other styles need a DOI, and this record has none. Use one of the built-in styles.";return}
    out.textContent="Loading...";try{const r=await fetch(`/api/cite?doi=${encodeURIComponent(m.doi)}&style=${encodeURIComponent(st)}`),j=await r.json();out.textContent=r.ok?j.citation:j.error}catch(e){out.textContent="Network error."}};
  b.querySelector(".copy").onclick=async e=>{const ok=await copyText(out.textContent);note.textContent=ok?"Copied!":"Copy failed, select the text manually.";};
}
async function bulk(fi){
  const f=data[fi],sel=document.querySelector(`[data-sel="${fi}"]`).value,note=document.querySelector(`[data-note="${fi}"]`);
  const ms=f.entries.filter(e=>e.match&&e.status==="verified"&&!e.retracted).map(e=>e.match);
  if(!ms.length){note.textContent="No verified references to copy.";return}
  const ok=await copyText(bulkText(ms,sel));
  const skipped=f.total-ms.length,ret=f.entries.filter(e=>e.status==="verified"&&e.retracted).length;
  note.textContent=(ok?`Copied ${ms.length} verified references.`:"Copy failed.")+(skipped?` ${skipped} skipped (check them manually).`:"")+(ret?` ${ret} retracted reference(s) excluded.`:"");
}
document.addEventListener("toggle",e=>{const d=e.target;if(d.matches&&d.matches("details.cite")&&d.open&&!d.dataset.ready){d.dataset.ready=1;buildCite(d)}},true);
$("run3").onclick=async()=>{
  const q=$("sq").value.trim();if(q.length<4)return say("m5","Type a title, authors or DOI.","err");
  prog(true);say("m5","Searching...");$("sout").innerHTML="";
  try{
    const r=await fetch("/api/search?q="+encodeURIComponent(q)),j=await r.json();if(!r.ok)throw new Error(j.error);
    say("m5",j.results.length?`${j.results.length} record(s) found.`:"No records found. Try fewer words, or the DOI.",j.results.length?"good":"err");
    $("sout").innerHTML=j.results.map(m=>`<div class="ref found"><span class="tag verified">Found in ${esc(m.src)}</span>${m.retracted?'<span class="tag ret">RETRACTED</span>':""}
      <b>${esc(m.title)}</b><div class="match">${esc(m.authors_list.slice(0,6).map(a=>(a[1]+" "+a[0]).trim()).join(", "))}${m.authors_list.length>6?" et al.":""} (${esc(m.year)}). ${esc(m.venue)}
      ${paperLink(m)}</div>${scopusTag(m)}${citeHtml(m)}</div>`).join("");
  }catch(e){say("m5",e.message,"err")}
  prog(false);
};
$("sq").onkeydown=e=>{if(e.key==="Enter")$("run3").click()};

let ME={},mode="login";
const RESET_TOKEN=new URLSearchParams(location.search).get("reset");
const money=(v,cur)=>{try{return new Intl.NumberFormat(undefined,{style:"currency",currency:cur||ME.currency||"USD"}).format(v)}catch(e){return (cur||"")+" "+v}};
function toast(t){const e=$("toast");e.textContent=t;e.classList.add("show");clearTimeout(toast.t);toast.t=setTimeout(()=>e.classList.remove("show"),3500)}
function prog(on){$("prog").hidden=!on}
document.querySelectorAll("dialog").forEach(d=>d.addEventListener("click",e=>{if(e.target===d)d.close()}));
document.addEventListener("click",e=>{const c=e.target.closest("[data-close]");if(c)c.closest("dialog").close()});

async function me(){
  ME=await (await fetch("/api/me")).json();
  document.querySelectorAll("[data-credit-only]").forEach(e=>e.hidden=!ME.credits_enabled);
  $("beta-info").hidden=ME.credits_enabled;
  $("beta-link").hidden=ME.credits_enabled;
  $("beta-limit").textContent=ME.guest_limit;
  $("who").textContent=!ME.credits_enabled?`Free beta: ${ME.left} left today`:ME.user?`${ME.left} file${ME.left===1?"":"s"} left`:`Guest: ${ME.left} of ${ME.guest_limit} left today`;
  $("authbtn").hidden=!!ME.user;$("accbtn").hidden=!ME.user;
  document.querySelectorAll(".pfree").forEach(e=>e.textContent=ME.free_total);document.querySelectorAll(".pguest").forEach(e=>e.textContent=ME.guest_limit);
  initGoogle();
  $("ppackf").textContent=$("payf").textContent=ME.pack_files;$("ppackp").textContent=$("payp").textContent=money(ME.pack_price);
}
/* ----- login / sign up / forgot / reset / email code ----- */
let pendingEmail="",coolT=null;
const TITLES={login:"Welcome back",signup:"Create your free account",forgot:"Reset your password",reset:"Choose a new password",verify:"Verify your email"};
const BTN={login:"Log in",signup:"Create account",forgot:"Send reset link",reset:"Save new password",verify:"Verify"};
const JSONH={"Content-Type":"application/json"};
function cooldown(sec){clearInterval(coolT);const b=$("resend");let s=sec;const tick=()=>{b.textContent=s>0?`Resend code (${s}s)`:"Resend code";b.disabled=s>0;if(s--<=0)clearInterval(coolT)};tick();coolT=setInterval(tick,1000)}
function setMode(m){
  mode=m;$("atitle").textContent=TITLES[m];$("asubmit").textContent=BTN[m];
  $("fEmail").hidden=m==="reset"||m==="verify";$("fPw").hidden=m==="forgot"||m==="verify";$("fCode").hidden=m!=="verify";
  $("apw").placeholder=m==="reset"?"New password (8+ characters)":"Password (8+ characters)";$("apw").autocomplete=m==="login"?"current-password":"new-password";
  document.querySelectorAll("[data-show]").forEach(el=>el.hidden=!el.dataset.show.split(" ").includes(m));$("gwrap").hidden=!(ME.google_client_id&&(m==="login"||m==="signup"));say("m4","");
  if(m==="verify"){$("codehelp").textContent=`We sent a 4-digit code to ${pendingEmail}. It expires in 10 minutes. Check your spam folder too.`;$("acode").value="";setTimeout(()=>$("acode").focus(),60);cooldown(60)}
}
function openAuth(m,msg){setMode(m);if(msg)say("m4",msg);if(!$("authdlg").open)$("authdlg").showModal();renderG()}
document.addEventListener("click",e=>{const g=e.target.closest("[data-go]");if(g)setMode(g.dataset.go);const o=e.target.closest("[data-open]");if(o)openAuth(o.dataset.open)});
$("authbtn").onclick=()=>openAuth("login");
$("acode").oninput=e=>{e.target.value=e.target.value.replace(/\D/g,"").slice(0,4);if(e.target.value.length===4)$("asubmit").click()};
$("asubmit").onclick=async()=>{
  if($("asubmit").disabled)return;
  const path={login:"/api/login",signup:"/api/register",forgot:"/api/forgot",reset:"/api/reset",verify:"/api/verify-email"}[mode];
  const body={email:mode==="verify"?pendingEmail:$("aemail").value.trim(),password:$("apw").value};
  if(mode==="reset")body.token=RESET_TOKEN;if(mode==="verify")body.code=$("acode").value;
  $("asubmit").disabled=true;
  try{
    const r=await fetch(path,{method:"POST",headers:JSONH,body:JSON.stringify(body)}),j=await r.json();
    if(r.status===403&&j.needs_verify){pendingEmail=j.email;setMode("verify");say("m4",j.note||j.error);return}
    if(!r.ok)throw new Error(j.error);
    if(mode==="signup"){pendingEmail=j.email;setMode("verify");if(j.note)say("m4",j.note,"good")}
    else if(mode==="forgot")say("m4",j.message,"good");
    else if(mode==="reset"){history.replaceState(null,"",location.pathname);setMode("login");say("m4","Password changed. You can log in now.","good")}
    else{$("authdlg").close();$("apw").value="";await me();toast(mode==="verify"?`Email verified.`:"Logged in.")}
  }catch(e){say("m4",e.message,"err")}
  finally{$("asubmit").disabled=false}
};
$("resend").onclick=async()=>{
  try{const r=await fetch("/api/resend-code",{method:"POST",headers:JSONH,body:JSON.stringify({email:pendingEmail})}),j=await r.json();
    if(!r.ok)throw new Error(j.error);say("m4",j.note||"A new code was sent.","good");cooldown(60)}catch(e){say("m4",e.message,"err")}
};
[$("aemail"),$("apw")].forEach(i=>i.onkeydown=e=>{if(e.key==="Enter")$("asubmit").click()});
/* ----- Continue with Google (Google proves the email is real) ----- */
let gLoaded=false;
function initGoogle(){
  if(!ME.google_client_id||gLoaded)return;gLoaded=true;
  const s=document.createElement("script");s.src="https://accounts.google.com/gsi/client";s.async=true;
  s.onload=()=>{google.accounts.id.initialize({client_id:ME.google_client_id,callback:onGoogle});renderG()};
  s.onerror=()=>{gLoaded=false;$("gwrap").hidden=true};document.head.appendChild(s);
}
function renderG(){
  if(!(window.google&&google.accounts&&google.accounts.id)||$("gbtn").dataset.r)return;
  $("gbtn").dataset.r=1;google.accounts.id.renderButton($("gbtn"),{theme:"outline",size:"large",text:"continue_with",width:280});
}
async function onGoogle(resp){
  try{
    const r=await fetch("/api/google-login",{method:"POST",headers:JSONH,body:JSON.stringify({credential:resp.credential})}),j=await r.json();
    if(!r.ok)throw new Error(j.error);$("authdlg").close();await me();toast(`Signed in with Google. You have ${ME.left} files.`);
  }catch(e){say("m4",e.message,"err")}
}
/* ----- account ----- */
$("accbtn").onclick=async()=>{
  try{
    const r=await fetch("/api/account"),j=await r.json();if(!r.ok)throw new Error(j.error);
    $("acc").innerHTML=`<span>Email</span><span>${esc(j.email)}</span>${ME.credits_enabled?`<span>Free files left</span><span>${j.free_left}</span><span>Paid credits</span><span>${j.credits}</span>`:""}<span>Joined</span><span>${esc(j.created)}</span><span>Last login</span><span>${esc(j.last_login||"-")}</span>`;
    $("pays").innerHTML=j.payments.length?`<table>${j.payments.map(p=>`<tr><td>${esc(p.ts)}<td>+${p.files} files<td>${p.amount?money(p.amount,p.cur):"Gift"}</tr>`).join("")}</table>`:"None yet.";
    say("m6","");$("accdlg").showModal();
  }catch(e){toast(e.message)}
};
$("outbtn").onclick=async()=>{await fetch("/api/logout",{method:"POST"});$("accdlg").close();await me();toast("Logged out.")};
$("chpw").onclick=async()=>{
  try{
    const r=await fetch("/api/change-password",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({old:$("oldpw").value,new:$("newpw").value})}),j=await r.json();
    if(!r.ok)throw new Error(j.error);say("m6","Password changed.","good");$("oldpw").value=$("newpw").value=""
  }catch(e){say("m6",e.message,"err")}
};
/* ----- payments ----- */
const PNAME={paypal:"Pay with PayPal or card",stripe:"Pay by card or online banking",lemon:"Pay securely (cards, PayPal) via Lemon Squeezy",link:"Pay (other method)"};
async function startPay(p){
  const m=$("paymsg");m.className="mute";m.textContent="Opening secure checkout, please wait...";$("provs").innerHTML="";
  if($("accdlg").open)$("accdlg").close();if(!$("paydlg").open)$("paydlg").showModal();
  try{
    const r=await fetch("/api/checkout",{method:"POST",headers:JSONH,body:JSON.stringify({provider:p})}),j=await r.json();
    if(!r.ok){const e=new Error(j.error);e.detail=j.detail;throw e}
    m.textContent=j.manual?"After you pay, send us your account email so we can add your credits. Taking you to the payment page...":"Redirecting to the payment page...";
    setTimeout(()=>location.href=j.url,j.manual?2500:150);
  }catch(e){
    m.className="msg err";m.textContent=e.message+(e.detail?"  [Owner details: "+e.detail+"]":"");
    $("provs").innerHTML=(ME.providers||[]).map(x=>`<button class="btn" data-prov="${esc(x)}">Try again: ${esc(PNAME[x]||x)}</button>`).join("");
  }
}
function openPay(msg){
  $("paymsg").className="mute";$("paymsg").textContent=msg||"Choose how you want to pay.";
  $("provs").innerHTML=(ME.providers||[]).map(p=>`<button class="btn" data-prov="${esc(p)}">${esc(PNAME[p]||p)}</button>`).join("")||'<a class="btn" href="#feedback" data-contact>Contact us to buy credits</a>';
  $("paydlg").showModal();
}
document.addEventListener("click",e=>{if(e.target.closest("[data-contact]")){$("paydlg").close();$("kind").value="contact"}const b=e.target.closest("[data-prov]");if(b){startPay(b.dataset.prov)}});
function buy(){
  if(!ME.credits_enabled)return;
  if(!ME.user){if($("paydlg").open)$("paydlg").close();return openAuth("signup","Create a free account first, then buy credits.")}
  const pr=ME.providers||[];if(!pr.length)return openPay("Online payment is not open yet. Send us a message and we will add credits to your account.");
  if(pr.length===1)return startPay(pr[0]);
  openPay();
}
document.addEventListener("click",e=>{if(e.target.closest("[data-buy]"))buy()});
/* ----- animations: slider, counters, reveal ----- */
const reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
(function(){
  const track=$("track"),slides=[...track.children],dots=$("dots");let i=0,timer,paused=false;
  slides.forEach((_,n)=>{const b=document.createElement("button");b.setAttribute("aria-label","Go to slide "+(n+1));b.onclick=()=>{go(n);restart()};dots.appendChild(b)});
  function go(n){i=(n+slides.length)%slides.length;track.style.transform=`translateX(-${i*100}%)`;
    slides.forEach((s,k)=>{s.classList.toggle("act",k===i);s.setAttribute("aria-hidden",k!==i)});[...dots.children].forEach((d,k)=>d.setAttribute("aria-current",k===i))}
  function restart(){clearInterval(timer);if(!reduce)timer=setInterval(()=>{if(!paused)go(i+1)},5200)}
  $("sprev").onclick=()=>{go(i-1);restart()};$("snext").onclick=()=>{go(i+1);restart()};
  const sl=$("sl");["mouseenter","focusin"].forEach(ev=>sl.addEventListener(ev,()=>paused=true));["mouseleave","focusout"].forEach(ev=>sl.addEventListener(ev,()=>paused=false));
  let x0=null;sl.addEventListener("touchstart",e=>x0=e.touches[0].clientX,{passive:true});
  sl.addEventListener("touchend",e=>{if(x0===null)return;const dx=e.changedTouches[0].clientX-x0;if(Math.abs(dx)>40){go(i+(dx<0?1:-1));restart()}x0=null});
  go(0);restart();
})();
function countUp(el){const to=+el.dataset.to;if(reduce){el.textContent=to;return}const t0=performance.now();
  (function f(t){const p=Math.min((t-t0)/1200,1);el.textContent=Math.round(to*(1-Math.pow(1-p,3)));if(p<1)requestAnimationFrame(f)})(t0)}
if("IntersectionObserver" in window){
  const io=new IntersectionObserver(es=>es.forEach(en=>{if(en.isIntersecting){en.target.classList.add("in");io.unobserve(en.target)}}),{threshold:.12});
  document.querySelectorAll(".rv").forEach(e=>io.observe(e));
  const co=new IntersectionObserver(es=>es.forEach(en=>{if(en.isIntersecting){countUp(en.target);co.unobserve(en.target)}}));
  document.querySelectorAll(".count").forEach(e=>co.observe(e));
}else{document.querySelectorAll(".rv").forEach(e=>e.classList.add("in"));document.querySelectorAll(".count").forEach(e=>e.textContent=e.dataset.to)}
/* ----- start-up ----- */
me().then(()=>{
  const q=new URLSearchParams(location.search);
  if(RESET_TOKEN)openAuth("reset");
  if(ME.credits_enabled&&q.get("paid")==="1"){
    const sid=q.get("session_id");history.replaceState(null,"",location.pathname);toast("Confirming your payment...");
    (async()=>{for(let n=0;n<6;n++){
      try{if(sid){const r=await fetch("/api/confirm-payment?session_id="+encodeURIComponent(sid)),j=await r.json();if(r.ok){await me();toast(`Payment confirmed. ${j.files} files are in your account.`);return}}}catch(e){}
      await me();await new Promise(r=>setTimeout(r,2500))}
      toast("Your payment is still processing. Credits appear automatically once it is confirmed.")})();
  }
  if(ME.credits_enabled&&q.get("pp")==="1"&&q.get("token")){
    const oid=q.get("token");history.replaceState(null,"",location.pathname);toast("Confirming your PayPal payment...");
    fetch("/api/paypal-return?order_id="+encodeURIComponent(oid)).then(r=>r.json().then(j=>({ok:r.ok,j}))).then(async({ok,j})=>{await me();toast(ok?`Payment confirmed. ${j.files} files are in your account.`:j.error)});
  }
  if(q.get("pp")==="0"){toast("Payment cancelled.");history.replaceState(null,"",location.pathname)}
  if(q.get("paid")==="0"){toast("Payment cancelled.");history.replaceState(null,"",location.pathname)}
});
let rating=0;
$("stars").innerHTML=[1,2,3,4,5].map(n=>`<button type="button" data-n="${n}" role="radio" aria-checked="false" aria-label="${n} stars">&#9733;</button>`).join("");
$("stars").onclick=e=>{const n=+e.target.dataset?.n;if(!n)return;rating=n;
  document.querySelectorAll("#stars button").forEach(b=>{b.classList.toggle("on",+b.dataset.n<=n);b.setAttribute("aria-checked",+b.dataset.n===n)})};
$("send").onclick=async()=>{
  try{
    const r=await fetch("/api/feedback",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({rating,kind:$("kind").value,message:$("fmsg").value,email:$("femail").value})});
    const j=await r.json();if(!r.ok)throw new Error(j.error);
    say("m3","Thank you! Your feedback was sent.","good");$("fmsg").value="";
  }catch(e){say("m3",e.message,"err")}
};
