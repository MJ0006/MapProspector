// Heat map rendering + "flip the map" for a scan. Usage: renderHeatmap(dataUrl)
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}
function rankClass(rank) {
  if (!rank) return "rn";
  if (rank <= 3) return "r1";
  if (rank <= 6) return "r2";
  if (rank <= 10) return "r3";
  return "r4";
}

async function renderHeatmap(dataUrl) {
  const data = await (await fetch(dataUrl)).json();
  const map = L.map("map").setView(data.center, 13);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    {maxZoom: 19, attribution: "&copy; OpenStreetMap"}).addTo(map);
  if (data.polygon) L.polygon(data.polygon, {color: "#2563eb", weight: 1, fillOpacity: 0.04}).addTo(map);
  const layer = L.layerGroup().addTo(map);

  function draw(ranks) {
    layer.clearLayers();
    data.points.forEach((p, i) => {
      const rank = ranks ? ranks[i] : p.rank;
      const label = !p.ok ? "!" : rank ? rank : "20+";
      const icon = L.divIcon({className: "", iconSize: [30, 30],
        html: `<div class="rank-pin ${p.ok ? rankClass(rank) : "rn"}">${label}</div>`});
      // Business names come from Google/DataForSEO: escape before Leaflet renders them as HTML.
      const tip = p.ok ? `Top 3 here: ${p.top.map(esc).join(", ") || "—"}` : "This point failed to load";
      L.marker([p.lat, p.lng], {icon}).bindTooltip(tip).addTo(layer);
    });
  }
  if (data.points.length) map.fitBounds(data.points.map(p => [p.lat, p.lng]), {padding: [24, 24]});
  draw(null);

  const list = document.getElementById("complist");
  if (!list) return;
  data.competitors.forEach(c => {
    const b = document.createElement("button");
    b.type = "button";
    b.innerHTML = `<span><b></b>${c.you ? ' <span class="badge info">you</span>' : ""}</span><span class="hint">avg ${c.avg_rank_all} · ${c.top3_pct}% top 3</span>`;
    b.querySelector("b").textContent = c.name;
    b.onclick = () => {
      list.querySelectorAll("button").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      document.getElementById("who").textContent = c.name;
      draw(c.ranks);
    };
    if (c.you) b.classList.add("active");
    list.appendChild(b);
  });
  if (!data.competitors.length) list.innerHTML = '<div class="empty">No results yet.</div>';
}
