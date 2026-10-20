// Assistant Pacewell : envoi de la question, lecture de la réponse en flux (Server-Sent Events
// sur une requête POST) et affichage des coulisses de chaque réponse.
(function () {
  "use strict";
  var fil = document.getElementById("fil");
  var form = document.getElementById("saisie");
  var champ = document.getElementById("question");
  var bouton = document.getElementById("envoyer");
  var intro = document.getElementById("intro");
  var vide = document.getElementById("coulisses-vide");
  var detail = document.getElementById("coulisses-detail");
  var historique = [];   // [{role, texte}] renvoyé au serveur pour les questions de suivi
  var reponses = [];     // coulisses de chaque réponse, pour y revenir

  function e(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Mise en forme minimale : paragraphes, listes « - », gras **…**, citations [n].
  function rendre(texte, idx) {
    var blocs = e(texte).trim().split(/\n\s*\n/);
    return blocs.map(function (b) {
      var lignes = b.split("\n");
      var html;
      if (lignes.every(function (l) { return /^\s*[-•*]\s+/.test(l); })) {
        html = "<ul>" + lignes.map(function (l) { return "<li>" + l.replace(/^\s*[-•*]\s+/, "") + "</li>"; }).join("") + "</ul>";
      } else {
        html = "<p>" + lignes.join("<br>") + "</p>";
      }
      return html
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/\[(\d+(?:\s*,\s*\d+)*)\]/g, function (_, ns) {
          return ns.split(",").map(function (n) {
            n = n.trim();
            return '<button type="button" class="cite" data-rep="' + idx + '" data-n="' + n + '" aria-label="Voir la source ' + n + '">' + n + "</button>";
          }).join("");
        });
    }).join("");
  }

  function ms(v) { return v == null ? "–" : v >= 1000 ? (v / 1000).toFixed(1).replace(".", ",") + " s" : v + " ms"; }

  function note(n) {
    if (n == null) return "";
    var s = '<span class="note" title="Note du reclassement : ' + n + ' sur 3">';
    for (var i = 1; i <= 3; i++) s += '<i class="' + (i <= n ? "plein" : "") + '"></i>';
    return s + "</span>";
  }

  function passageHtml(p, num) {
    var m = [];
    if (p.rang_vecteur) m.push("sens : n°" + p.rang_vecteur + (p.similarite != null ? " (" + p.similarite.toFixed(2).replace(".", ",") + ")" : ""));
    if (p.rang_texte) m.push("mots : n°" + p.rang_texte);
    if (!p.rang_vecteur) m.push("absent par le sens");
    if (!p.rang_texte) m.push("absent par les mots");
    return '<div class="passage" data-num="' + (num || "") + '">' +
      '<div class="entete"><span>' + (num ? '<span class="num">' + num + "</span>" : "") +
      '<span class="titre">' + e(p.titre_document) + " › " + e(p.section) + "</span></span>" + note(p.note) + "</div>" +
      (num ? '<p class="extrait">' + e(p.extrait) + "</p>" : "") +
      '<div class="mesures">' + m.map(function (x) { return "<span>" + e(x) + "</span>"; }).join("") + "</div></div>";
  }

  function afficherCoulisses(idx) {
    var r = reponses[idx];
    if (!r || !r.passages) return;
    vide.hidden = true; detail.hidden = false;
    [].forEach.call(document.querySelectorAll(".msg-assistant"), function (el) { el.classList.toggle("choisi", +el.dataset.idx === idx); });
    var p = r.passages, f = r.fin || {};
    var h = "";
    if (p.question_recherche && p.question_recherche !== r.question) {
      h += "<h3>Question reformulée pour la recherche</h3><p class=\"reformulee\">" + e(p.question_recherche) + "</p>";
    }
    h += "<h3>Passages donnés à l'IA (" + p.retenus.length + ")</h3>";
    h += p.retenus.length ? p.retenus.map(function (x, i) { return passageHtml(x, i + 1); }).join("") : "<p>Aucun passage assez pertinent.</p>";
    if (p.ecartes && p.ecartes.length) {
      h += '<details class="ecartes"><summary>' + p.ecartes.length + " candidats écartés</summary>" + p.ecartes.map(function (x) { return passageHtml(x); }).join("") + "</details>";
    }
    if (r.fin) {
      var d = f.durees_ms || {};
      var etapes = [["Reformulation", d.reformulation], ["Recherche", d.recherche], ["Reclassement", d.reclassement], ["Premier mot", d.premier_mot], ["Rédaction", d.redaction]]
        .filter(function (x) { return x[1] != null; });
      var max = Math.max.apply(null, etapes.map(function (x) { return x[1]; }).concat([1]));
      h += "<h3>Chiffres de cette réponse</h3><div class=\"chiffres\">" +
        "<div><b>" + ms(d.total) + "</b><span>temps total</span></div>" +
        "<div><b>" + (f.cout_usd != null ? (f.cout_usd * 100).toFixed(2).replace(".", ",") + " ct" : "–") + "</b><span>coût estimé (dollars)</span></div>" +
        "<div><b>" + ((f.jetons || {}).entree || 0).toLocaleString("fr-FR") + "</b><span>jetons lus</span></div>" +
        "<div><b>" + ((f.jetons || {}).sortie || 0).toLocaleString("fr-FR") + "</b><span>jetons écrits</span></div></div>";
      h += etapes.map(function (x) {
        return '<div class="duree"><span>' + x[0] + '</span><span class="barre"><i style="width:' + Math.max(3, x[1] / max * 100) + '%"></i></span><span>' + ms(x[1]) + "</span></div>";
      }).join("");
    }
    detail.innerHTML = h;
  }

  function surlignerSource(idx, n) {
    afficherCoulisses(idx);
    [].forEach.call(document.querySelectorAll(".cite"), function (c) { c.classList.toggle("actif", +c.dataset.rep === idx && c.dataset.n === n); });
    var cible = detail.querySelector('.passage[data-num="' + n + '"]');
    [].forEach.call(detail.querySelectorAll(".passage"), function (x) { x.classList.toggle("cite-actif", x === cible); });
    if (cible) cible.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  fil.addEventListener("click", function (ev) {
    var c = ev.target.closest(".cite");
    if (c) return surlignerSource(+c.dataset.rep, c.dataset.n);
    var v = ev.target.closest(".voir-coulisses");
    if (v) afficherCoulisses(+v.dataset.idx);
  });

  function ajouter(classe, html) {
    var li = document.createElement("li");
    li.className = classe; li.innerHTML = html;
    fil.appendChild(li);
    li.scrollIntoView({ block: "end", behavior: "smooth" });
    return li;
  }

  async function poser(question) {
    intro.hidden = true;
    bouton.disabled = true;
    ajouter("msg-client", e(question));
    var idx = reponses.length;
    var r = { question: question, texte: "" };
    reponses.push(r);
    var li = ajouter("msg-assistant", '<div class="corps"><p class="attente">Recherche dans la documentation…</p></div>');
    li.dataset.idx = idx;
    var corps = li.querySelector(".corps");
    try {
      var rep = await fetch("/api/question", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: question, historique: historique.slice(-6) })
      });
      if (!rep.ok) {
        var err = await rep.json().catch(function () { return {}; });
        throw new Error(err.erreur || "L'assistant ne répond pas pour le moment.");
      }
      var lecteur = rep.body.getReader(), dec = new TextDecoder(), tampon = "";
      for (;;) {
        var lu = await lecteur.read();
        if (lu.done) break;
        tampon += dec.decode(lu.value, { stream: true });
        var parts = tampon.split("\n\n");
        tampon = parts.pop();
        parts.forEach(function (bloc) {
          if (bloc.indexOf("data: ") !== 0) return;
          var ev = JSON.parse(bloc.slice(6));
          if (ev.type === "etape" && ev.etape === "redaction" && !r.texte) corps.innerHTML = '<p class="attente">Rédaction de la réponse…</p>';
          else if (ev.type === "passages") { r.passages = ev; afficherCoulisses(idx); }
          else if (ev.type === "texte") { r.texte += ev.texte; corps.innerHTML = rendre(r.texte, idx); }
          else if (ev.type === "erreur") { corps.insertAdjacentHTML("beforeend", '<p class="msg-erreur">' + e(ev.message) + "</p>"); }
          else if (ev.type === "fin") {
            r.fin = ev;
            if (ev.sources && ev.sources.length) {
              li.insertAdjacentHTML("beforeend", '<div class="sources">' + ev.sources.map(function (s) {
                return '<a href="https://pacewell.simondelrue.fr" title="Page fictive de Pacewell" data-n="' + s.numero + '"><b>' + s.numero + "</b>" + e(s.titre) + " › " + e(s.section) + "</a>";
              }).join("") + "</div>");
            }
            li.insertAdjacentHTML("beforeend", '<button type="button" class="voir-coulisses" data-idx="' + idx + '">Voir les coulisses de cette réponse</button>');
            afficherCoulisses(idx);
          }
        });
      }
      historique.push({ role: "user", texte: question }, { role: "assistant", texte: r.texte });
    } catch (err) {
      corps.innerHTML = '<p class="msg-erreur">' + e(err.message) + "</p>";
    } finally {
      bouton.disabled = false;
      champ.focus();
    }
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var q = champ.value.trim();
    if (!q || bouton.disabled) return;
    champ.value = ""; champ.style.height = "";
    poser(q);
  });
  champ.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); form.requestSubmit(); }
  });
  champ.addEventListener("input", function () { champ.style.height = "auto"; champ.style.height = Math.min(champ.scrollHeight, 160) + "px"; });
  [].forEach.call(document.querySelectorAll(".suggestions button"), function (b) {
    b.addEventListener("click", function () { poser(b.textContent.replace(/ /g, " ")); });
  });
  // Les liens de sources pointent vers la landing : la page d'aide n'existe pas (marque fictive).
  fil.addEventListener("click", function (ev) {
    var a = ev.target.closest(".sources a");
    if (a) { ev.preventDefault(); surlignerSource(+a.closest(".msg-assistant").dataset.idx, a.dataset.n); }
  });
})();
