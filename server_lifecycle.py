"""Local shutdown notification only; never starts, stops or resumes processing."""

import asyncio
import os
import secrets
import time

from fastapi import Request
from starlette.responses import Response, StreamingResponse

TOKEN_ENV = "ANYTHINGLLM_PDF_ASSISTANT_STOP_TOKEN"


def install_lifecycle_routes(app):
    token = os.environ.get(TOKEN_ENV, "")
    clients = {}
    pending_until = 0.0

    async def watch():
        async def events():
            queue = asyncio.Queue(maxsize=2)
            delivered = asyncio.Event()
            clients[queue] = delivered
            try:
                event = "stop_requested" if time.monotonic() < pending_until else "ready"
                yield f"data: {event}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), 15)
                        yield f"data: {event}\n\n"
                        delivered.set()
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                clients.pop(queue, None)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def notify(request: Request):
        nonlocal pending_until
        supplied = request.headers.get("X-Assistant-Stop-Token", "")
        if not token or not secrets.compare_digest(supplied.encode(), token.encode()):
            return Response(status_code=403)
        event = request.path_params["event"]
        if event not in {"stop_requested", "stop_cancelled"}:
            return Response(status_code=400)
        pending_until = time.monotonic() + 20 if event == "stop_requested" else 0.0

        async def deliver(queue, ack):
            ack.clear()
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
            await ack.wait()

        # One global bound, independent of tab count or a suspended browser.
        if clients:
            try:
                await asyncio.wait_for(asyncio.gather(*(deliver(s, a) for s, a in list(clients.items()))), 0.4)
                await asyncio.sleep(0.05)
            except asyncio.TimeoutError:
                pass
        return Response(status_code=204)

    app.add_api_route("/assistant-lifecycle", watch, methods=["GET"], include_in_schema=False)
    app.add_api_route("/assistant-lifecycle/{event}", notify, methods=["POST"], include_in_schema=False)


LIFECYCLE_HEAD = r"""
<script id="rag-assistant-lifecycle">
document.addEventListener('DOMContentLoaded', () => {
  let pendingUntil = 0;
  const oldMessage = 'Connection to the server was lost. Attempting reconnection...';
  const newMessage = 'Connection to the assistant was unexpectedly lost. Attempting reconnection…';
  function relabel(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (node.nodeValue === oldMessage) node.nodeValue = newMessage;
    }
  }
  new MutationObserver(records => {
    for (const record of records) {
      if (record.type === 'characterData') {
        if (record.target.nodeValue === oldMessage) record.target.nodeValue = newMessage;
      } else for (const node of record.addedNodes) {
        if (node.nodeType === Node.TEXT_NODE) {
          if (node.nodeValue === oldMessage) node.nodeValue = newMessage;
        } else relabel(node);
      }
    }
  }).observe(document.body, {subtree: true, childList: true, characterData: true});
  relabel(document.body);
  function connect() {
    const socket = new EventSource('/assistant-lifecycle');
    socket.onmessage = event => {
      const kind = event.data;
      pendingUntil = kind === 'stop_requested' ? Date.now() + 20000 : 0;
    };
    socket.onerror = () => {
      if (pendingUntil > Date.now()) {
        socket.close();
        // Unload the Gradio document, including its private reconnect timers.
        // The replacement has no scripts or network resources and never polls.
        const doc = document.implementation.createHTMLDocument('Assistant stopped');
        const style = doc.createElement('style');
        style.textContent = 'body{font:16px Segoe UI,sans-serif;margin:48px;background:#eef2f7;color:#1e293b}p{margin:24px 0}';
        doc.head.append(style);
        const message = doc.createElement('p');
        message.textContent = 'Assistant stopped through user intervention. Connection broken.';
        doc.body.append(message);
        location.replace(URL.createObjectURL(new Blob(['<!doctype html>' + doc.documentElement.outerHTML], {type:'text/html'})));
      }
    };
  }
  connect();
});
</script>
"""
