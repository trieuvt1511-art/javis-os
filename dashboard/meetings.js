/* Explicit meeting recording. Audio stays local; summary sends only text to the configured model. */
(function () {
  "use strict";
  let dialog, rows, status, start, stop, pick, language, recorder, stream, current;
  let pending = Promise.resolve(), failure = null, index = 0, timer, active = false;
  const t = key => { const full = "meeting." + key; return window.t ? window.t(full) : key; };
  const brain = () => (window.JavisSessions && window.JavisSessions.brain()) || "brain";
  let selectedBrain, lastRows = "";
  function url(path) { return "/meetings" + path + "?brain=" + encodeURIComponent(selectedBrain); }
  async function api(path, method, body, json) {
    const response = await fetch(url(path), {method:method || "GET", body:body,
      credentials:"same-origin", headers:json ? {"Content-Type":"application/json"} : {}});
    if (!response.ok) throw new Error((await response.text()).slice(0, 300));
    return response.json();
  }
  function note(value) { status.textContent = value; }
  function busy(value) { active = value; start.disabled = pick.disabled = language.disabled = value; }
  function localStore(mode, action) {
    return new Promise((resolve,reject) => {
      const request=indexedDB.open("javis-meeting-audio",1);
      request.onupgradeneeded=()=>request.result.createObjectStore("chunks",{keyPath:"key"});
      request.onerror=()=>reject(request.error);
      request.onsuccess=()=>{
        const db=request.result, tx=db.transaction("chunks",mode), store=tx.objectStore("chunks");
        const result=action(store);
        tx.oncomplete=()=>{db.close();resolve(result.result);};
        tx.onerror=()=>{db.close();reject(tx.error);};
      };
    });
  }
  const saveLocal=(id,seq,blob)=>localStore("readwrite",s=>s.put({key:id+":"+seq,id,seq,blob,brain:selectedBrain}));
  const deleteLocal=(id,seq)=>localStore("readwrite",s=>s.delete(id+":"+seq));
  async function recover(row) {
    const chunks=(await localStore("readonly",s=>s.getAll())).filter(c=>c.id===row.id && c.brain===selectedBrain).sort((a,b)=>a.seq-b.seq);
    for(const chunk of chunks) {
      await api("/"+row.id+"/chunks/"+chunk.seq,"POST",chunk.blob);
      await deleteLocal(row.id,chunk.seq);
    }
    await api("/"+row.id+"/finish","POST");await refresh();
  }
  function link(row, file, label, media) {
    const el = document.createElement(media ? "audio" : "a");
    const target = url("/" + row.id + "/files/" + encodeURIComponent(file));
    if (media) { el.controls = true; el.src = target; el.preload = "none"; }
    else { el.href = target; el.target = "_blank"; el.rel = "noopener"; el.textContent = label; }
    return el;
  }
  function button(label, action) {
    const el = document.createElement("button"); el.type = "button"; el.textContent = label;
    el.onclick = () => Promise.resolve().then(action).catch(e => note(e.message));
    return el;
  }
  async function refresh() {
    try {
      const data = await api(""); const signature = JSON.stringify(data);
      if (signature === lastRows) return;
      lastRows = signature; rows.replaceChildren();
      for (const row of data) {
        const box = document.createElement("section");
        const label = document.createElement("p");
        label.textContent = (row.title || row.id.slice(0,8)) + " · " + t(row.state) + (row.error ? " · " + row.error : "");
        box.append(label);
        if (row.audio) box.append(link(row, row.audio, t("audio"), true));
        if (row.transcript) box.append(link(row, row.transcript, t("transcript")));
        if (row.minutes) box.append(link(row, row.minutes, t("minutes")));
        if (row.state === "recording" && (!current || current.id !== row.id)) {
          box.append(button(t("recover"), async () => { await recover(row); }));
        } else if (row.audio && !["converting","transcribing","summarizing"].includes(row.state)) {
          box.append(button(t("transcribe"), async () => { await api("/"+row.id+"/transcribe", "POST"); await refresh(); }));
          if (row.transcript) box.append(button(t("summarize"), async () => { await api("/"+row.id+"/summarize", "POST"); await refresh(); }));
        }
        rows.append(box);
      }
    } catch (error) { note(error.message); }
  }
  async function uploadChunk(blob, seq) {
    for (let attempt = 0; attempt < 3; attempt++) {
      try { const data = await api("/"+current.id+"/chunks/"+seq, "POST", blob); await deleteLocal(current.id,seq); return data; }
      catch (error) { if (attempt === 2) throw error; await new Promise(resolve => setTimeout(resolve, 500)); }
    }
  }
  function stopTracks() { if (stream) stream.getTracks().forEach(track => track.stop()); stream = null; }
  async function begin() {
    busy(true); note(t("permission")); failure = null; index = 0; pending = Promise.resolve();
    try {
      if (!navigator.mediaDevices || !window.MediaRecorder) throw new Error(t("unsupported"));
      // Called only from this explicit button. Never resume recording on reload.
      stream = await navigator.mediaDevices.getUserMedia({audio:true});
      const mime = ["audio/webm;codecs=opus","audio/mp4","audio/ogg;codecs=opus"].find(m => MediaRecorder.isTypeSupported(m));
      if (!mime) throw new Error(t("unsupported"));
      current = await api("", "POST", JSON.stringify({title:new Date().toLocaleString(),language:language.value,mime:mime.split(";")[0]}), true);
      recorder = new MediaRecorder(stream, {mimeType:mime});
      recorder.ondataavailable = event => {
        if (!event.data.size) return;
        const seq = index++;
        const saved = saveLocal(current.id,seq,event.data); saved.catch(() => {});
        pending = pending.then(() => saved).then(() => uploadChunk(event.data, seq)).catch(error => {
          failure = error; note(t("saveError") + " " + error.message);
          if (recorder && recorder.state === "recording") recorder.stop();
          throw error;
        });
        pending.catch(() => {});
      };
      recorder.onerror = event => { failure = event.error || new Error(t("recordError")); note(failure.message); if(recorder.state!=="inactive") recorder.stop(); };
      recorder.onstop = async () => {
        stopTracks(); stop.disabled = true;
        try { await pending; if (failure) throw failure; await api("/"+current.id+"/finish", "POST"); note(t("saved")); }
        catch(error) { note(t("saveError") + " " + error.message); }
        finally { recorder = null; current = null; busy(false); await refresh(); }
      };
      recorder.start(1000); stop.disabled = false; note(t("recording"));
    } catch(error) { stopTracks(); current=null; recorder=null; busy(false); note(error.message); }
  }
  async function upload(file) {
    if (!file) return;
    busy(true); note(t("uploading"));
    try {
      if (!file.size || file.size > 512*1024*1024) throw new Error(t("tooLarge"));
      const extensions = {wav:"audio/wav",mp3:"audio/mpeg",webm:"audio/webm",m4a:"audio/mp4",mp4:"audio/mp4",ogg:"audio/ogg",flac:"audio/flac"};
      const mime = extensions[file.name.split(".").pop().toLowerCase()];
      if (!mime) throw new Error(t("unsupported"));
      current = await api("", "POST", JSON.stringify({title:file.name,language:language.value,mime:mime}), true);
      let seq = 0;
      for (let offset=0;offset<file.size;offset+=1024*1024) await uploadChunk(file.slice(offset,offset+1024*1024),seq++);
      await api("/"+current.id+"/finish", "POST"); note(t("saved"));
    } finally { current=null; busy(false); await refresh(); }
  }
  function open() {
    if (dialog) { dialog.showModal(); return; }
    selectedBrain = brain(); lastRows = "";
    dialog = document.createElement("dialog"); dialog.id="meetingDialog";
    dialog.style.cssText="width:min(680px,90vw);max-height:85vh;overflow:auto;padding:20px;border-radius:16px";
    const title=document.createElement("h2");title.textContent=t("title");dialog.append(title);
    const info=document.createElement("button"), text=document.createElement("div");
    info.type="button";info.className="info-i";info.textContent="i";info.setAttribute("aria-label",t("privacyLabel"));
    text.className="info-body";text.hidden=true;text.textContent=t("privacy");dialog.append(info,text);
    language=document.createElement("select"); language.id="meetingLanguage";
    for(const code of ["vi","es","en","auto"]) { const option=document.createElement("option");option.value=code;option.textContent=t(code);language.append(option); }
    language.setAttribute("aria-label",t("language"));dialog.append(language);
    start=button(t("start"),begin);start.id="meetingStart";
    stop=button(t("stop"),()=>{if(recorder && recorder.state!=="inactive"){stop.disabled=true;recorder.stop();}});stop.id="meetingStop";stop.disabled=true;
    const input=document.createElement("input");input.type="file";input.accept="audio/*,.m4a,.flac";input.hidden=true;input.id="meetingFile";
    input.onchange=()=>upload(input.files[0]).catch(error=>note(error.message));
    pick=button(t("upload"),()=>input.click());pick.id="meetingUpload";
    const close=button(t("close"),()=>{if(active || recorder || current) {note(t("stopFirst"));return;} clearInterval(timer);dialog.close();dialog.remove();dialog=null;});
    dialog.addEventListener("cancel",event=>{event.preventDefault();close.click();});
    status=document.createElement("p");status.setAttribute("role","status");rows=document.createElement("div");rows.id="meetingRows";
    dialog.append(start,stop,pick,close,input,status,rows);document.body.append(dialog);dialog.showModal();refresh();timer=setInterval(refresh,2000);
  }
  window.addEventListener("beforeunload",event=>{if(active || recorder || current){event.preventDefault();event.returnValue="";}});
  window.JavisMeetings={open:open};
  if(window.JavisComposerMenu) window.JavisComposerMenu.register({id:"meetings",order:45,label:"meeting.title",run:open});
})();
