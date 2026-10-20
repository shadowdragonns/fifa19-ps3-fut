'use strict';
const $ = id => document.getElementById(id);
const number = value => new Intl.NumberFormat('en-GB').format(value);
const state = {me:null, options:null, quote:null, size:3, edition:'standard', register:false, busy:false, serial:0};
let debounce;
async function api(path, data) {
  const options = {credentials:'same-origin', headers:{}};
  if (data !== undefined) {
    options.method = 'POST'; options.headers['Content-Type'] = 'application/json';
    if (state.me) options.headers['X-CSRF-Token'] = state.me.csrf;
    options.body = JSON.stringify(data);
  }
  let response;
  try { response = await fetch('/club/api/'+path, options); }
  catch (_) { throw new Error('Could not reach your club. Please try again.'); }
  let result;
  try { result = await response.json(); }
  catch (_) { throw new Error('The server returned an unexpected response. Please try again.'); }
  if (!response.ok) {
    if (response.status === 401 && state.me) signedOut();
    throw new Error(result.error || 'Something went wrong. Please try again.');
  }
  return result;
}
function element(tag, text, cls) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (cls) item.className = cls;
  return item;
}
function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
function balance() {
  $('coins').textContent = state.me ? number(state.me.coins) : '—';
  $('club-name').textContent = state.me ? state.me.club_name : 'Your club awaits';
  $('club-user').textContent = state.me ? '@'+state.me.username : 'Sign in to get started';
  $('club-initials').textContent = state.me ? state.me.club_name.split(/\s+/).map(s=>s[0]).join('').slice(0,2).toUpperCase() : 'FC';
  $('owned-count').textContent = state.me ? number(state.me.card_count)+' players in your club' : 'Your next signing starts here';
}
function signedOut() {
  state.me=null; state.quote=null; state.serial++;
  $('auth-panel').hidden=false; $('filters').disabled=true; $('reset').disabled=true; $('logout').hidden=true;
  $('open-pack').disabled=true; $('refresh-quote').disabled=true; $('price').textContent='—'; $('breakdown').replaceChildren();
  $('quote-summary').textContent='Sign in to start building.'; $('matches').textContent='Your club, your possibilities';
  $('history-list').replaceChildren(element('p','Your first custom pack is waiting to be built.','empty'));
  balance();
}
async function loadClub() {
  state.me = await api('me'); balance();
  $('auth-panel').hidden=true; $('filters').disabled=false; $('reset').disabled=false; $('logout').hidden=false;
  if (!state.options) {
    state.options=await api('options');
    for (const name of ['clubs','nations','leagues']) {
      $(name).replaceChildren(...state.options[name].map(row=>{const opt=document.createElement('option');opt.value=row.name;return opt;}));
    }
    for (const pos of state.options.positions) {
      const option=document.createElement('option'); option.value=pos; option.textContent=pos; $('position').append(option);
    }
  }
  history();
}
function choice(field, list) {
  const value=$(field).value.trim();
  if (!value) return null;
  const row=state.options[list].find(item=>item.name.toLowerCase()===value.toLowerCase());
  if (!row) throw new Error('Choose a '+(field==='nation'?'nationality':field)+' from the list.');
  return row.id;
}
function parameters() {
  return {size:state.size,edition:state.edition,level:$('level').value,nation:choice('nation','nations'),
    club:choice('club','clubs'),league:choice('league','leagues'),position:$('position').value||null,
    min_rating:Number($('min-rating').value),rare_only:$('rare-only').checked};
}
function chooseButtons() {
  document.querySelectorAll('[data-size]').forEach(button=>{
    const selected=Number(button.dataset.size)===state.size;
    button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));
  });
  document.querySelectorAll('[data-edition]').forEach(button=>{
    const selected=button.dataset.edition===state.edition;
    button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));
  });
  $('pack-count').textContent=state.size+' PLAYER'+(state.size===1?'':'S');
  $('art-edition').textContent=state.edition==='prime'?'PRIME':'19';
  document.querySelector('.pack-art').classList.toggle('prime-art',state.edition==='prime');
}
function enableOpen() {
  const q=state.quote;
  $('refresh-quote').disabled=state.busy||!state.me;
  $('open-pack').disabled=state.busy||!state.me||!q||!q.available||q.price>state.me.coins||!$('duplicate-consent').checked;
}
async function updateQuote() {
  if (!state.me||state.busy) return;
  const serial=++state.serial;
  state.quote=null;enableOpen();$('quote-error').textContent='';$('matches').textContent='Finding your players…';
  try {
    const p=parameters();
    try { localStorage.setItem('fut-lab-pack-draft',JSON.stringify(p)); } catch (_) {}
    const q=await api('quote',p);
    if (serial!==state.serial||!state.me) return;
    state.quote=q;
    $('quote-title').textContent=(p.edition==='prime'?'Prime':'Your custom')+' player pack';
    $('quote-summary').textContent=p.size+' distinct '+(p.edition==='prime'?'Prime ':'')+'player'+(p.size===1?'':'s')+(p.min_rating?' rated '+p.min_rating+'+':'')+'. Built around your selections.';
    $('matches').textContent=number(q.matching_players)+' matching player'+(q.matching_players===1?'':'s');
    $('price').textContent=number(q.price);
    $('breakdown').replaceChildren(...q.breakdown.map(row=>{
      const line=element('div');line.append(element('span',row.label),element('span',number(row.coins)));return line;
    }));
    $('quote-error').textContent=!q.available?'Not enough distinct players. Try broadening your filters.':q.price>state.me.coins?'You need '+number(q.price-state.me.coins)+' more FUT coins.':'';
    enableOpen();
  } catch (error) {
    if (serial!==state.serial) return;
    $('matches').textContent='Your selection needs a little adjustment';
    $('quote-error').textContent=error.message; $('price').textContent='—';$('breakdown').replaceChildren();
  }
}
function scheduleQuote() {clearTimeout(debounce);state.quote=null;state.serial++;enableOpen();debounce=setTimeout(updateQuote,350);}
function applyDraft() {
  try {
    const draft=JSON.parse(localStorage.getItem('fut-lab-pack-draft')||'null');
    if (!draft||![1,3,6,12].includes(draft.size)||!['standard','prime'].includes(draft.edition)) return;
    state.size=draft.size;state.edition=draft.edition;
    if (['any','gold','silver','bronze'].includes(draft.level)) $('level').value=draft.level;
    for (const [id,key,list] of [['club','club','clubs'],['nation','nation','nations'],['league','league','leagues']]) {
      const row=state.options[list].find(r=>r.id===draft[key]);$(id).value=row?row.name:'';
    }
    $('position').value=draft.position||'';
    $('min-rating').value=String(draft.min_rating||0);if(!$('min-rating').value)$('min-rating').value='0';
    $('rare-only').checked=draft.rare_only===true;chooseButtons();
  } catch (_) {}
}
function history() {
  const rows=state.me.history||[];
  if (!rows.length) {$('history-list').replaceChildren(element('p','No packs opened yet. Your next signing starts here.','empty'));return;}
  $('history-list').replaceChildren(...rows.map(row=>{
    const item=element('div',undefined,'history-row');
    const prime=row.parameters.edition==='prime';
    const art=element('span',prime?'✦':'19','mini-pack'+(prime?' prime':''));
    const description=element('div');description.append(element('strong',row.cards.length+' '+(prime?'Prime':'custom')+' players'),element('p',row.cards.map(c=>c.name).join(' · ')));
    const date=element('time',new Date(row.opened_at*1000).toLocaleDateString('en-GB',{day:'numeric',month:'short'}));
    item.append(art,description,date,element('span','−'+number(row.price)));return item;
  }));
}
function playerCard(card) {
  const item=element('article',undefined,'player-card'+(card.edition==='prime'?' prime':''));
  const top=element('div',undefined,'card-top');
  const rating=element('div');rating.append(element('div',String(card.rating),'rating'),element('div',card.preferredPosition,'position'));
  top.append(rating,element('span',card.edition==='prime'?'PRIME':'FUT 19','card-edition'));
  const initials=card.name.replace(/^Prime /,'').split(/\s+/).map(s=>s[0]).join('').slice(0,2);
  const club=state.options.clubs.find(row=>row.id===card.teamid);
  const nation=state.options.nations.find(row=>row.id===card.nation);
  item.append(top,element('div',initials,'portrait'),element('h3',card.name.replace(/^Prime /,'')),element('p',(club?club.name:'')+' · '+(nation?nation.name:''),'card-club'));
  const labels=card.preferredPosition==='GK'?['DIV','HAN','KIC','REF','SPD','POS']:['PAC','SHO','PAS','DRI','DEF','PHY'];
  const stats=element('div',undefined,'card-stats');
  card.attributeList.forEach((stat,i)=>{const value=element('span');value.append(element('strong',String(stat.value)),document.createTextNode(labels[i]));stats.append(value);});
  item.append(stats,element('small',card.duplicate?'DUPLICATE · +'+number(card.quick_sell_coins):'ADDED TO CLUB','card-status'));
  return item;
}
$('auth-switch').addEventListener('click',()=>{
  state.register=!state.register;$('invite-label').hidden=!state.register;$('invite').required=state.register;
  $('auth-title').textContent=state.register?'Make it your club':'Sign in to your club';
  $('auth-submit').textContent=state.register?'Create web account ↗':'Sign in ↗';
  $('auth-switch').textContent=state.register?'Already linked? Sign in':'First time? Link your club';
  $('password').minLength=state.register?12:1;$('password').autocomplete=state.register?'new-password':'current-password';$('auth-error').textContent='';
});
$('auth-form').addEventListener('submit',async event=>{
  event.preventDefault();$('auth-error').textContent='';$('auth-submit').disabled=true;
  try {
    const data={username:$('username').value.trim(),password:$('password').value};
    if(state.register)data.invite=$('invite').value.trim();
    await api(state.register?'register':'login',data);$('password').value='';$('invite').value='';
    await loadClub();applyDraft();await updateQuote();
  } catch(error) {$('auth-error').textContent=error.message;}
  finally {$('auth-submit').disabled=false;}
});
$('logout').addEventListener('click',async()=>{try{await api('logout',{});signedOut();notice('You have signed out.');}catch(error){notice(error.message);}});
document.querySelectorAll('[data-size]').forEach(button=>button.addEventListener('click',()=>{state.size=Number(button.dataset.size);chooseButtons();scheduleQuote();}));
document.querySelectorAll('[data-edition]').forEach(button=>button.addEventListener('click',()=>{state.edition=button.dataset.edition;chooseButtons();scheduleQuote();}));
for (const id of ['club','nation','league','position','level','min-rating','rare-only']) $(id).addEventListener('input',scheduleQuote);
$('duplicate-consent').addEventListener('change',enableOpen);
$('refresh-quote').addEventListener('click',updateQuote);
$('reset').addEventListener('click',()=>{state.size=3;state.edition='standard';for(const id of ['club','nation','league','position'])$(id).value='';$('level').value='gold';$('min-rating').value='0';$('rare-only').checked=false;chooseButtons();scheduleQuote();});
$('open-pack').addEventListener('click',async()=>{
  if(state.busy||!state.quote)return;
  state.busy=true;enableOpen();$('open-pack').textContent='Opening your pack…';$('quote-error').textContent='';
  try {
    const result=await api('open',{quote_token:state.quote.quote_token,duplicate_consent:$('duplicate-consent').checked});
    $('reveal-cards').replaceChildren(...result.cards.map(playerCard));
    const kept=result.cards.filter(c=>!c.duplicate).length;
    $('reveal-summary').textContent=kept+' player'+(kept===1?'':'s')+' added to your club'+(result.duplicate_refund?' · '+number(result.duplicate_refund)+' coins returned for duplicates':'')+'.';
    $('reveal').showModal();state.quote=null;
    state.me=await api('me');balance();history();
    notice('Pack saved to your FUT club. Refresh your club in game to see the new players.');
  } catch(error){$('quote-error').textContent=error.message;}
  finally{state.busy=false;$('open-pack').textContent='Open this pack ↗';enableOpen();}
});
$('close-reveal').addEventListener('click',()=>{$('reveal').close();updateQuote();});
$('reveal').addEventListener('cancel',()=>{updateQuote();});
async function refreshBalance() {
  if(!state.me||state.busy||document.hidden)return;
  try{state.me=await api('me');balance();history();enableOpen();}catch(_){}
}
document.addEventListener('visibilitychange',refreshBalance);setInterval(refreshBalance,15000);
(async()=>{chooseButtons();try{await loadClub();applyDraft();await updateQuote();}catch(error){signedOut();if(error.message!=='Sign in to your club.')notice(error.message);}})();
