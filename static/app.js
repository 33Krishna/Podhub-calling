// State
let calls = [];
let selectedCallId = null;
let lastEventCursor = 0;
let eventSource = null;

// DOM Elements
const phoneInput = document.getElementById("phone-input");
const startBtn = document.getElementById("start-btn");
const formError = document.getElementById("form-error");
const callsList = document.getElementById("calls-list");
const callCount = document.getElementById("call-count");
const emptyState = document.getElementById("empty-state");
const detailsCard = document.getElementById("details-card");
const sseDot = document.getElementById("sse-dot");
const sseText = document.getElementById("sse-text");

const transcriptCard = document.getElementById("transcript-card");
const transcriptCount = document.getElementById("transcript-count");
const transcriptContainer = document.getElementById("transcript-container");
const transcriptEmpty = document.getElementById("transcript-empty");
const transcriptMessages = document.getElementById("transcript-messages");

// Per-call transcript storage: Map<callId, Map<eventId, {event_id, speaker, text}>>
const callTranscripts = new Map();

const detailPhone = document.getElementById("detail-phone");
const detailBadge = document.getElementById("detail-badge");
const detailId = document.getElementById("detail-id");
const detailRoom = document.getElementById("detail-room");
const detailProviderCall = document.getElementById("detail-provider-call");
const detailWorkerState = document.getElementById("detail-worker-state");
const detailPollTime = document.getElementById("detail-poll-time");
const detailCreated = document.getElementById("detail-created");
const detailError = document.getElementById("detail-error");

function generateUUID() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
    const r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
    return v.toString(16);
  });
}

function formatTime(isoStr) {
  if (!isoStr) return "Never";
  try {
    const d = new Date(isoStr);
    return d.toLocaleTimeString() + " (" + d.toLocaleDateString() + ")";
  } catch (e) {
    return isoStr;
  }
}

function renderBadge(status) {
  const cls = "badge badge-" + (status || "unknown");
  return `<span class="${cls}">${status || "unknown"}</span>`;
}

function scrollTranscriptToBottom() {
  if (transcriptContainer) {
    transcriptContainer.scrollTop = transcriptContainer.scrollHeight;
  }
}

function createTranscriptElement(item) {
  const msgDiv = document.createElement("div");
  const isAgent = item.speaker === "agent";
  msgDiv.className = "transcript-msg " + (isAgent ? "transcript-msg-agent" : "transcript-msg-prospect");
  msgDiv.dataset.eventId = item.event_id;

  const headerDiv = document.createElement("div");
  headerDiv.className = "transcript-msg-header";

  const speakerSpan = document.createElement("span");
  speakerSpan.className = "speaker-badge " + (isAgent ? "speaker-badge-agent" : "speaker-badge-prospect");
  speakerSpan.textContent = isAgent ? "Ananya (Agent)" : "Prospect";

  headerDiv.appendChild(speakerSpan);

  const textDiv = document.createElement("div");
  textDiv.className = "transcript-msg-text";
  // Strict textContent ONLY - no innerHTML for transcript text
  textDiv.textContent = item.text || "";

  msgDiv.appendChild(headerDiv);
  msgDiv.appendChild(textDiv);
  return msgDiv;
}

function addTranscriptItems(callId, items) {
  if (!callTranscripts.has(callId)) {
    callTranscripts.set(callId, new Map());
  }
  const itemsMap = callTranscripts.get(callId);
  let addedCount = 0;
  for (const item of items) {
    if (!item || item.event_id === undefined || item.event_id === null) continue;
    const eventId = Number(item.event_id);
    if (!itemsMap.has(eventId)) {
      itemsMap.set(eventId, {
        event_id: eventId,
        speaker: item.speaker || "unknown",
        text: item.text || ""
      });
      addedCount++;
    }
  }
  return addedCount;
}

function renderTranscript(callId) {
  if (callId !== selectedCallId) return;

  const itemsMap = callTranscripts.get(callId);
  const items = itemsMap ? Array.from(itemsMap.values()) : [];
  items.sort((a, b) => a.event_id - b.event_id);

  transcriptCount.textContent = `${items.length} ${items.length === 1 ? "message" : "messages"}`;

  if (items.length === 0) {
    transcriptEmpty.style.display = "block";
    transcriptMessages.innerHTML = "";
    return;
  }

  transcriptEmpty.style.display = "none";

  const currentElements = transcriptMessages.querySelectorAll(".transcript-msg");

  let needFullReRender = false;
  if (currentElements.length > items.length) {
    needFullReRender = true;
  } else {
    for (let i = 0; i < currentElements.length; i++) {
      if (Number(currentElements[i].dataset.eventId) !== items[i].event_id) {
        needFullReRender = true;
        break;
      }
    }
  }

  if (needFullReRender) {
    transcriptMessages.innerHTML = "";
    items.forEach(item => {
      transcriptMessages.appendChild(createTranscriptElement(item));
    });
    scrollTranscriptToBottom();
  } else {
    let appended = false;
    for (let i = currentElements.length; i < items.length; i++) {
      transcriptMessages.appendChild(createTranscriptElement(items[i]));
      appended = true;
    }
    if (appended) {
      scrollTranscriptToBottom();
    }
  }
}

function updateDetailsPanel(call) {
  if (!call) {
    emptyState.style.display = "block";
    detailsCard.style.display = "none";
    transcriptCard.style.display = "none";
    return;
  }
  emptyState.style.display = "none";
  detailsCard.style.display = "block";
  transcriptCard.style.display = "flex";

  detailPhone.textContent = call.phone_number;
  detailBadge.className = "badge badge-" + (call.status || "unknown");
  detailBadge.textContent = call.status || "unknown";

  detailId.textContent = call.id;
  detailRoom.textContent = call.room_id || "Pending";
  detailProviderCall.textContent = call.provider_call_id || "Pending";
  detailWorkerState.textContent = call.worker_state || "waiting";
  detailPollTime.textContent = formatTime(call.last_provider_poll_at);
  detailCreated.textContent = formatTime(call.created_at);

  if (call.error_message) {
    detailError.style.display = "block";
    const status = call.status || "unknown";
    if (status === "no_answer" || status === "busy") {
      detailError.className = "detail-alert detail-alert-warning";
      detailError.textContent = "Reason: " + call.error_message;
    } else {
      detailError.className = "detail-alert detail-alert-danger";
      detailError.textContent = "Error: " + call.error_message;
    }
  } else {
    detailError.style.display = "none";
  }

  if (call.transcript && Array.isArray(call.transcript)) {
    addTranscriptItems(call.id, call.transcript);
  }
  renderTranscript(call.id);
}

function renderCallsList() {
  callsList.innerHTML = "";
  callCount.textContent = calls.length;

  calls.forEach(call => {
    const li = document.createElement("li");
    li.className = "call-item" + (call.id === selectedCallId ? " active" : "");
    li.onclick = () => selectCall(call.id);

    li.innerHTML = `
      <div class="call-item-top">
        <span class="call-phone">${call.phone_number}</span>
        <span class="call-time">${formatTime(call.created_at)}</span>
      </div>
      <div>
        ${renderBadge(call.status)}
      </div>
    `;
    callsList.appendChild(li);
  });
}

async function selectCall(callId) {
  selectedCallId = callId;
  renderCallsList();
  renderTranscript(callId);
  try {
    const res = await fetch(`/calls/${callId}`);
    if (res.ok) {
      const call = await res.json();
      const idx = calls.findIndex(c => c.id === callId);
      if (idx !== -1) calls[idx] = { ...calls[idx], ...call };
      updateDetailsPanel(call);
    }
  } catch (err) {
    console.error("Failed to fetch call details:", err);
  }
}

async function loadCalls() {
  try {
    const res = await fetch("/calls?limit=50");
    if (res.ok) {
      const data = await res.json();
      calls = data.calls || [];
      if (data.event_cursor) lastEventCursor = data.event_cursor;
      renderCallsList();
      if (calls.length > 0 && !selectedCallId) {
        selectCall(calls[0].id);
      }
    }
  } catch (err) {
    console.error("Failed to load calls:", err);
  }
}

function initSSE() {
  if (eventSource) {
    eventSource.close();
  }

  sseDot.className = "status-dot";
  sseText.textContent = "Connecting...";

  const url = `/calls/stream?after=${lastEventCursor}`;
  eventSource = new EventSource(url);

  eventSource.onopen = () => {
    sseDot.className = "status-dot connected";
    sseText.textContent = "Live";
  };

  eventSource.onerror = () => {
    sseDot.className = "status-dot disconnected";
    sseText.textContent = "Reconnecting";
  };

  // Listen strictly to call.updated events
  eventSource.addEventListener("call.updated", async (event) => {
    try {
      if (event.lastEventId) {
        lastEventCursor = parseInt(event.lastEventId, 10) || lastEventCursor;
      }
      const payload = JSON.parse(event.data);
      const callId = payload.call_id;
      const newStatus = payload.status;

      // Update call in list
      const callIdx = calls.findIndex(c => c.id === callId);
      if (callIdx !== -1) {
        if (newStatus) calls[callIdx].status = newStatus;
        if (payload.worker_state) calls[callIdx].worker_state = payload.worker_state;
        renderCallsList();
      } else {
        // New call just arrived, reload list
        await loadCalls();
      }

      // If current selected call is updated, refresh its details
      if (selectedCallId === callId) {
        selectCall(callId);
      }

      // Re-fetch latest call data from GET /calls/{id} to update list and detail panel
      if (callId) {
        try {
          const res = await fetch(`/calls/${callId}`);
          if (res.ok) {
            const freshCall = await res.json();
            const idx = calls.findIndex(c => c.id === callId);
            if (idx !== -1) {
              calls[idx] = { ...calls[idx], ...freshCall };
              renderCallsList();
            }
            if (selectedCallId === callId) {
              updateDetailsPanel(freshCall);
            }
          }
        } catch (fetchErr) {
          console.error("Failed to refresh call details:", fetchErr);
        }
      }
    } catch (err) {
      console.error("Error processing call.updated event:", err);
    }
  });

  // Listen to transcript.final events
  eventSource.addEventListener("transcript.final", (event) => {
    try {
      if (event.lastEventId) {
        lastEventCursor = parseInt(event.lastEventId, 10) || lastEventCursor;
      }
      const data = JSON.parse(event.data);
      const callId = data.call_id;
      if (!callId) return;

      const added = addTranscriptItems(callId, [data]);
      if (added > 0 && selectedCallId === callId) {
        renderTranscript(callId);
      }
    } catch (err) {
      console.error("Error processing transcript.final event:", err);
    }
  });
}

// Start Call Submission
startBtn.onclick = async () => {
  formError.style.display = "none";
  const phone = phoneInput.value.trim();

  if (!phone.startsWith("+") || phone.length < 9) {
    formError.textContent = "Please enter a valid international phone number starting with '+' (e.g. +91XXXXXXXXXX)";
    formError.style.display = "block";
    return;
  }

  // Rule: Disable button immediately to prevent double-click
  startBtn.disabled = true;
  startBtn.textContent = "Dialing...";

  const idempotencyKey = generateUUID();

  try {
    const res = await fetch("/calls", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        phone_number: phone,
        idempotency_key: idempotencyKey
      })
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      const msg = (errData.detail && errData.detail.error) || "Failed to initiate call";
      formError.textContent = msg;
      formError.style.display = "block";
      startBtn.disabled = false;
      startBtn.textContent = "Start Call";
      return;
    }

    const data = await res.json();
    await loadCalls();
    selectCall(data.id);

    // Reset button after success
    startBtn.disabled = false;
    startBtn.textContent = "Start Call";
  } catch (err) {
    formError.textContent = "Network error while connecting to server.";
    formError.style.display = "block";
    startBtn.disabled = false;
    startBtn.textContent = "Start Call";
  }
};

// Initialize
loadCalls().then(() => {
  initSSE();
});

