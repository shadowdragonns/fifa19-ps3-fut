'use strict';
const $ = id => document.getElementById(id);
const number = value => new Intl.NumberFormat('en-GB').format(value);
const state = {me:null, options:null, quote:null, size:3, edition:'standard', register:false, busy:false, serial:0};
let debounce;
const editionLabel = edition => ({england110:'England 110',standard:'Standard',prime:'Prime / TOTW',toty:'Team of the Year',otw:'Ones to Watch'}[edition] || edition);
const editionMark = edition => ({standard:'19',prime:'PRIME',toty:'TOTY',otw:'OTW'}[edition] || 'FUT');
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
  $('contract-settings').hidden=!state.me;
  if(state.me?.contracts){
    const c=state.me.contracts;
    $('auto-contracts').checked=c.auto_renew;
    $('contract-description').textContent=`Uses owned contract cards first. If none remain, pays ${number(c.player_price)} coins per player (+${c.player_matches} matches) or ${number(c.manager_price)} per manager (+${c.manager_matches}). Applies at zero contracts to your active XI, bench and selected manager between matches. Insufficient coins leave the card unchanged.`;
    const r=c.last_renewal;
    $('contract-recent').textContent=r?`Last renewal: ${r.items.length} card${r.items.length===1?'':'s'}, ${number(r.coins)} coins spent.`:'';
  }
}
let contractsBusy=false;
$('auto-contracts').addEventListener('change',async()=>{
  if(!state.me||contractsBusy)return;
  contractsBusy=true;$('auto-contracts').disabled=true;$('contract-status').textContent='Saving…';
  try{
    await api('contracts',{auto_renew:$('auto-contracts').checked});
    state.me=await api('me');balance();enableOpen();
    $('contract-status').textContent=state.me.contracts.auto_renew?'Automatic renewal enabled.':'Automatic renewal disabled.';
  }catch(error){balance();$('contract-status').textContent=error.message;}
  finally{contractsBusy=false;$('auto-contracts').disabled=false;}
});
function signedOut() {
  state.me=null; state.quote=null; state.serial++;
  adminAccess();
  $('auth-panel').hidden=false; $('filters').disabled=true; $('reset').disabled=true; $('logout').hidden=true;
  $('open-pack').disabled=true; $('refresh-quote').disabled=true; $('price').textContent='—'; $('breakdown').replaceChildren();
  $('quote-summary').textContent='Sign in to start building.'; $('matches').textContent='Your club, your possibilities';
  $('history-list').replaceChildren(element('p','Your first custom pack is waiting to be built.','empty'));
  balance();
}
async function loadClub() {
  state.me = await api('me'); balance();
  adminAccess();
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
  for (const button of document.querySelectorAll('[data-edition] small')) {
    const edition=button.parentElement.dataset.edition;
    if(state.options.editions?.[edition])button.textContent='BASE +'+state.options.editions[edition].boost;
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
  $('art-edition').textContent=editionMark(state.edition);
  document.querySelector('.pack-art').dataset.edition=state.edition;
  document.querySelector('.pack-art').classList.toggle('prime-art',state.edition!=='standard');
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
    $('quote-title').textContent=(p.edition!=='standard'?editionLabel(p.edition):'Your custom')+' player pack';
    $('quote-summary').textContent=p.size+' distinct '+(p.edition!=='standard'?editionLabel(p.edition)+' ':'')+'player'+(p.size===1?'':'s')+(p.min_rating?' rated '+p.min_rating+'+':'')+'. Built around your selections.';
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
    if (!draft||![1,3,6,12].includes(draft.size)||!['standard','prime','toty','otw'].includes(draft.edition)) return;
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
    const prime=row.parameters.edition!=='standard';
    const art=element('span',prime?'✦':'19','mini-pack'+(prime?' prime':''));
    const description=element('div');description.append(element('strong',row.cards.length+' '+(prime?editionLabel(row.parameters.edition):'custom')+' players'),element('p',row.cards.map(c=>c.name).join(' · ')));
    const date=element('time',new Date(row.opened_at*1000).toLocaleDateString('en-GB',{day:'numeric',month:'short'}));
    item.append(art,description,date,element('span','−'+number(row.price)));return item;
  }));
}
function playerCard(card) {
  const item=element('article',undefined,'player-card'+(card.edition!=='standard'?' prime '+card.edition:''));
  const top=element('div',undefined,'card-top');
  const rating=element('div');rating.append(element('div',String(card.rating),'rating'),element('div',card.preferredPosition,'position'));
  top.append(rating,element('span',editionMark(card.edition),'card-edition'));
  const initials=card.name.replace(/^(Prime|Team of the Year|Ones to Watch) /,'').split(/\s+/).map(s=>s[0]).join('').slice(0,2);
  const club=state.options.clubs.find(row=>row.id===card.teamid);
  const nation=state.options.nations.find(row=>row.id===card.nation);
  item.append(top,element('div',initials,'portrait'),element('h3',card.name.replace(/^(Prime|Team of the Year|Ones to Watch) /,'')),element('p',(club?club.name:'')+' · '+(nation?nation.name:''),'card-club'));
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
  if(!state.me||state.busy||contractsBusy||document.hidden)return;
  try{state.me=await api('me');balance();history();enableOpen();}catch(_){}
}
document.addEventListener('visibilitychange',refreshBalance);setInterval(refreshBalance,15000);
const admin = {ready:false,clubs:[],cards:[],current:null,page:0,total:0,serial:0,busy:false};
let adminDebounce;
function adminAccess() {
  const allowed=Boolean(state.me?.is_admin);
  $('admin-link').hidden=!allowed;$('admin-editor').hidden=!allowed;
  if(!allowed){admin.ready=false;admin.current=null;admin.serial++;$('admin-fields').disabled=true;$('admin-cards').replaceChildren();return;}
  if(!admin.ready){admin.ready=true;loadAdminClubs().catch(error=>{$('admin-status').textContent=error.message;admin.ready=false;});}
}
async function loadAdminClubs() {
  const result=await api('admin/clubs');
  if(!state.me?.is_admin)return;
  admin.clubs=result.clubs;
  $('admin-club').replaceChildren(...result.clubs.map(row=>{
    const opt=element('option',row.name+' · '+row.player);opt.value=String(row.persona);return opt;
  }));
  if(result.clubs.some(row=>row.persona===state.me.persona))$('admin-club').value=String(state.me.persona);
  admin.page=0;await loadAdminCards(true);
}
async function loadAdminCards(resetSquads=false) {
  if(admin.busy||!state.me?.is_admin||!$('admin-club').value)return;
  const serial=++admin.serial;
  admin.current=null;$('admin-fields').disabled=true;$('admin-status').textContent='Loading owned cards…';
  const data={persona:Number($('admin-club').value),squad_id:resetSquads?null:($('admin-squad').value?Number($('admin-squad').value):null),search:$('admin-search').value,page:admin.page};
  try {
    const result=await api('admin/inventory',data);
    if(serial!==admin.serial||!state.me?.is_admin)return;
    admin.cards=result.cards;admin.total=result.total;
    const selected=resetSquads?'':$('admin-squad').value;
    $('admin-squad').replaceChildren(element('option','All owned players'));
    $('admin-squad').firstChild.value='';
    for(const row of result.squads){const opt=element('option',row.name+(row.active?' (active)':''));opt.value=String(row.id);$('admin-squad').append(opt);}
    $('admin-squad').value=selected;
    $('admin-position').replaceChildren(...result.positions.map(p=>{const opt=element('option',p);opt.value=p;return opt;}));
    $('admin-cards').replaceChildren(...result.cards.map(row=>{const opt=element('option',row.rating+' · '+row.name+' · '+editionLabel(row.edition)+' · #'+row.id);opt.value=String(row.id);return opt;}));
    $('admin-count').textContent=number(result.total)+' cards · page '+(admin.page+1);
    $('admin-prev').disabled=admin.page===0;$('admin-next').disabled=(admin.page+1)*100>=result.total;
    $('admin-status').textContent=result.cards.length?'Select a card to edit.':'No owned players match this selection.';
    if(result.cards.length)selectAdminCard(result.cards[0]);
  }catch(error){if(serial===admin.serial)$('admin-status').textContent=error.message;}
}
function adminAttributeLabels() {
  const labels=$('admin-position').value==='GK'?['DIV','HAN','KIC','REF','SPD','POS']:['PAC','SHO','PAS','DRI','DEF','PHY'];
  document.querySelectorAll('#admin-attributes label span').forEach((label,i)=>{label.textContent=labels[i];});
}
function selectAdminCard(row) {
  admin.current=row;$('admin-cards').value=String(row.id);$('admin-fields').disabled=false;
  $('admin-player').textContent=row.name+' · '+editionLabel(row.edition);
  $('admin-card-info').textContent='Card #'+row.id+(Object.keys(row.overrides).length?' · stat overrides saved':' · edition stats')+(row.training?' · temporary training active':'');
  $('admin-rating').value=row.rating;$('admin-position').value=row.position;
  $('admin-attributes').replaceChildren(...row.attributes.map((value,i)=>{const label=element('label');label.append(element('span'));const input=element('input');input.id='admin-attr-'+i;input.type='number';input.min='0';input.max='4294967295';input.required=true;input.value=value;label.append(input);return label;}));
  for(const key of ['fitness','contract','suspension','injuryGames','injuryType'])$('admin-'+key).value=row[key];
  adminAttributeLabels();
}
async function saveAdminCard(reset=false) {
  if(admin.busy||!admin.current)return;
  clearTimeout(adminDebounce);
  admin.busy=true;
  for(const id of ['admin-club','admin-squad','admin-search','admin-cards','admin-refresh','admin-prev','admin-next'])$(id).disabled=true;
  $('admin-fields').disabled=true;$('admin-status').textContent='Saving card…';
  const row=admin.current;
  const condition=reset?{}:Object.fromEntries(['fitness','contract','suspension','injuryGames','injuryType'].map(key=>[key,key==='injuryType'?$('admin-'+key).value:Number($('admin-'+key).value)]).filter(([key,value])=>value!==row[key]));
  const overrides=reset?{}:{...row.overrides};
  if(!reset){
    const rating=Number($('admin-rating').value),position=$('admin-position').value;
    const attributes=Array.from({length:6},(_,i)=>Number($('admin-attr-'+i).value));
    if(rating!==row.rating)overrides.rating=rating;
    if(position!==row.position)overrides.position=position;
    if(attributes.some((value,i)=>value!==row.attributes[i]))overrides.attributes=attributes;
  }
  try {
    const result=await api('admin/edit',{persona:Number($('admin-club').value),item_id:row.id,revision:row.revision,overrides,condition});
    if(!state.me?.is_admin)return;
    admin.cards=admin.cards.map(card=>card.id===result.card.id?result.card:card);selectAdminCard(result.card);
    const option=Array.from($('admin-cards').options).find(opt=>Number(opt.value)===row.id);
    if(option)option.textContent=result.card.rating+' · '+result.card.name+' · '+editionLabel(result.card.edition)+' · #'+row.id;
    $('admin-status').textContent=result.changed?'Saved. Reopen your squad in FIFA to refresh this card.':'No changes to save.';
  }catch(error){$('admin-status').textContent=error.message;}
  finally{admin.busy=false;$('admin-fields').disabled=!admin.current||!state.me?.is_admin;
    for(const id of ['admin-club','admin-squad','admin-search','admin-cards','admin-refresh'])$(id).disabled=false;
    $('admin-prev').disabled=admin.page===0;$('admin-next').disabled=(admin.page+1)*100>=admin.total;}
}
$('admin-form').addEventListener('submit',event=>{event.preventDefault();saveAdminCard();});
$('admin-reset').addEventListener('click',()=>saveAdminCard(true));
$('admin-position').addEventListener('change',adminAttributeLabels);
$('admin-club').addEventListener('change',()=>{admin.page=0;loadAdminCards(true);});
$('admin-squad').addEventListener('change',()=>{admin.page=0;loadAdminCards();});
$('admin-search').addEventListener('input',()=>{clearTimeout(adminDebounce);adminDebounce=setTimeout(()=>{admin.page=0;loadAdminCards();},300);});
$('admin-cards').addEventListener('change',()=>{const row=admin.cards.find(card=>card.id===Number($('admin-cards').value));if(row)selectAdminCard(row);});
$('admin-prev').addEventListener('click',()=>{if(admin.page>0){admin.page--;loadAdminCards();}});
$('admin-next').addEventListener('click',()=>{if((admin.page+1)*100<admin.total){admin.page++;loadAdminCards();}});
$('admin-refresh').addEventListener('click',()=>loadAdminCards());

(async()=>{chooseButtons();try{await loadClub();applyDraft();await updateQuote();}catch(error){signedOut();if(error.message!=='Sign in to your club.')notice(error.message);}})();

function updateNavigation(){document.querySelectorAll('.sidebar .nav-link').forEach(link=>link.classList.toggle('active',link.getAttribute('href')===(location.hash||'#studio')));}
window.addEventListener('hashchange',updateNavigation);updateNavigation();
