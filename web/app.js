// Assistant Pacewell : envoi de la question (et d'une photo éventuelle), lecture de la réponse en flux
// (Server-Sent Events sur une requête POST) et affichage des coulisses de chaque réponse.
(function () {
  "use strict";
  var fil = document.getElementById("fil");
  var form = document.getElementById("saisie");
  var champ = document.getElementById("question");
  var bouton = document.getElementById("envoyer");
  var intro = document.getElementById("intro");
  var vide = document.getElementById("coulisses-vide");
  var detail = document.getElementById("coulisses-detail");
  var fichier = document.getElementById("fichier");
  var piece = document.getElementById("piece");
  var pieceApercu = document.getElementById("piece-apercu");
  var pieceNom = document.getElementById("piece-nom");
  var historique = [];   // [{role, texte}] renvoyé au serveur pour les questions de suivi
  var reponses = [];     // coulisses de chaque réponse, pour y revenir
  var image = null;      // {donnees, type, apercu, nom}

  var LIBELLES = {
    chercher_documentation: "Recherche dans la documentation",
    consulter_commande: "Consultation de la commande",
    ouvrir_retour: "Ouverture d'un retour",
    transmettre_a_un_humain: "Transmission à un conseiller"
  };
  var EN_COURS = {
    chercher_documentation: "Recherche dans la documentation…",
    consulter_commande: "Consultation de la commande…",
    ouvrir_retour: "Vérification du retour…",
    transmettre_a_un_humain: "Transmission à un conseiller…"
  };

  function e(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Mise en forme minimale : paragraphes, listes « - », gras **…**, citations [n].
  function rendre(texte, idx) {
    var blocs = e(texte).trim().split(/\n\s*\n/);
    return blocs.map(function (b) {
      // Dans un bloc, les lignes « - » consécutives deviennent une liste, les autres un paragraphe.
      var lignes = b.split("\n").filter(function (l) { return l.trim(); });
      var html = "", liste = [], para = [];
      var item = /^\s*([-•*]|\d+[.)])\s+/;
      function vider() {
        if (para.length) { html += "<p>" + para.join("<br>") + "</p>"; para = []; }
        if (liste.length) { html += "<ul>" + liste.map(function (l) { return "<li>" + l + "</li>"; }).join("") + "</ul>"; liste = []; }
      }
      lignes.forEach(function (l) {
        if (item.test(l)) { if (para.length) vider(); liste.push(l.replace(item, "")); }
        else { if (liste.length) vider(); para.push(l); }
      });
      vider();
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

  function valeur(v) {
    if (Array.isArray(v)) return v.map(valeur).join(", ");
    if (v && typeof v === "object") return Object.keys(v).map(function (k) { return k + " : " + valeur(v[k]); }).join(" ; ");
    if (v === true) return "oui";
    if (v === false) return "non";
    return v == null ? "–" : String(v);
  }

  function documentHtml(d) {
    if (!d) return "";
    var lignes = [["Type", d.type_document], ["Enseigne", d.marque], ["N° de commande", d.numero_commande], ["Date", d.date],
      ["Articles", (d.articles || []).map(function (a) { return a.nom + (a.variante ? " (" + a.variante + ")" : ""); }).join(", ")],
      ["Total", d.total != null ? d.total + " €" : null], ["Lisible", d.lisible]];
    return "<h3>Document lu (vision + sortie structurée)</h3><dl class=\"champs\">" + lignes.map(function (l) {
      return "<dt>" + e(l[0]) + "</dt><dd>" + e(valeur(l[1])) + "</dd>";
    }).join("") + "</dl>";
  }

  function traceHtml(t, i) {
    var h = '<li class="trace"><div class="trace-tete"><span class="num">' + (i + 1) + '</span><b>' + e(LIBELLES[t.outil] || t.outil) +
      '</b><span class="duree-outil">' + ms(t.duree_ms) + "</span></div>";
    h += '<p class="appel"><code>' + e(t.outil) + "(" + e(Object.keys(t.entree || {}).map(function (k) {
      return k + "=" + valeur(t.entree[k]);
    }).join(", ")) + ")</code></p>";
    h += '<p class="resultat">' + e(t.resultat) + "</p>";
    if (t.outil === "chercher_documentation" && t.retenus) {
      h += t.retenus.map(function (p, j) { return passageHtml(p, t.premier_numero + j); }).join("");
      if (t.ecartes && t.ecartes.length) {
        h += '<details class="ecartes"><summary>' + t.ecartes.length + " candidats écartés</summary>" +
          t.ecartes.map(function (p) { return passageHtml(p); }).join("") + "</details>";
      }
    }
    return h + "</li>";
  }

  function afficherCoulisses(idx) {
    var r = reponses[idx];
    if (!r) return;
    vide.hidden = true; detail.hidden = false;
    [].forEach.call(document.querySelectorAll(".msg-assistant"), function (el) { el.classList.toggle("choisi", +el.dataset.idx === idx); });
    var h = documentHtml(r.document);
    h += "<h3>Outils appelés par l'agent</h3>";
    h += r.traces.length ? '<ol class="traces">' + r.traces.map(traceHtml).join("") + "</ol>"
                         : (r.fin ? "<p class=\"vide-outils\">Aucun : l'agent a répondu directement.</p>" : "<p class=\"vide-outils\">…</p>");
    if (r.fin) {
      var f = r.fin, d = f.durees_ms || {}, j = f.jetons || {};
      var lus = (j.entree || 0) + (j.cache_lecture || 0) + (j.cache_ecriture || 0);
      var etapes = [["Lecture photo", d.lecture_document], ["Premier mot", d.premier_mot], ["Modèle", d.modele], ["Outils", d.outils]]
        .filter(function (x) { return x[1] != null; });
      var max = Math.max.apply(null, etapes.map(function (x) { return x[1]; }).concat([1]));
      h += "<h3>Chiffres de cette réponse</h3><div class=\"chiffres\">" +
        "<div><b>" + ms(d.total) + "</b><span>temps total</span></div>" +
        "<div><b>" + (f.cout_usd != null ? (f.cout_usd * 100).toFixed(2).replace(".", ",") + " ct" : "–") + "</b><span>coût estimé (dollars)</span></div>" +
        "<div><b>" + lus.toLocaleString("fr-FR") + "</b><span>jetons lus, dont " + (lus ? Math.round((j.cache_lecture || 0) / lus * 100) : 0) + " % en cache</span></div>" +
        "<div><b>" + (j.sortie || 0).toLocaleString("fr-FR") + "</b><span>jetons écrits</span></div></div>";
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
    var a = ev.target.closest(".sources a");
    if (a) { ev.preventDefault(); return surlignerSource(+a.closest(".msg-assistant").dataset.idx, a.dataset.n); }
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

  // --- Photo : réduite dans le navigateur (1600 px au plus) avant l'envoi -------------------------------
  function preparerImage(blob, nom) {
    return new Promise(function (ok, ko) {
      var url = URL.createObjectURL(blob), img = new Image();
      img.onload = function () {
        var k = Math.min(1, 1600 / Math.max(img.width, img.height));
        var c = document.createElement("canvas");
        c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
        c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
        URL.revokeObjectURL(url);
        var dataUrl = c.toDataURL("image/jpeg", 0.85);
        ok({ donnees: dataUrl.split(",")[1], type: "image/jpeg", apercu: dataUrl, nom: nom });
      };
      img.onerror = function () { URL.revokeObjectURL(url); ko(new Error("Image illisible.")); };
      img.src = url;
    });
  }
  function joindre(im) {
    image = im;
    piece.hidden = !im;
    if (im) { pieceApercu.src = im.apercu; pieceNom.textContent = im.nom; }
  }
  fichier.addEventListener("change", function () {
    var f = fichier.files[0];
    fichier.value = "";
    if (!f) return;
    if (f.size > 15 * 1024 * 1024) return alerte("Photo trop lourde (15 Mo au plus).");
    preparerImage(f, f.name).then(joindre).catch(function (err) { alerte(err.message); });
  });
  document.getElementById("piece-retirer").addEventListener("click", function () { joindre(null); });
  function alerte(m) { ajouter("msg-erreur", e(m)); }

  // --- Envoi et lecture du flux -------------------------------------------------------------------
  async function poser(question, im) {
    intro.hidden = true;
    bouton.disabled = true;
    ajouter("msg-client", (im ? '<img class="miniature" src="' + im.apercu + '" alt="Photo jointe">' : "") + e(question));
    var idx = reponses.length;
    var r = { question: question, texte: "", traces: [], document: null };
    reponses.push(r);
    var li = ajouter("msg-assistant", '<div class="corps"><p class="attente">' + (im ? "Lecture de la photo…" : "Réflexion…") + "</p></div>");
    li.dataset.idx = idx;
    var corps = li.querySelector(".corps");
    var statut = null;
    function etat(t) {
      if (!statut) { statut = document.createElement("p"); statut.className = "attente"; li.appendChild(statut); }
      statut.textContent = t; statut.hidden = !t;
    }
    try {
      var rep = await fetch("/api/question", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: question, historique: historique.slice(-8),
                               image: im ? { donnees: im.donnees, type: im.type } : null })
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
          if (ev.type === "document") { r.document = ev.champs; afficherCoulisses(idx); }
          else if (ev.type === "outil") { etat(EN_COURS[ev.nom] || "Outil en cours…"); }
          else if (ev.type === "outil_resultat") { r.traces.push(ev); etat(""); afficherCoulisses(idx); }
          else if (ev.type === "texte") { r.texte += ev.texte; corps.innerHTML = rendre(r.texte, idx); }
          else if (ev.type === "erreur") { corps.insertAdjacentHTML("beforeend", '<p class="msg-erreur">' + e(ev.message) + "</p>"); }
          else if (ev.type === "fin") {
            r.fin = ev; etat("");
            if (!r.texte.trim()) corps.innerHTML = "";
            if (ev.sources && ev.sources.length) {
              li.insertAdjacentHTML("beforeend", '<div class="sources">' + ev.sources.map(function (s) {
                return '<a href="#" data-n="' + s.numero + '"><b>' + s.numero + "</b>" + e(s.titre) + " › " + e(s.section) + "</a>";
              }).join("") + "</div>");
            }
            li.insertAdjacentHTML("beforeend", '<button type="button" class="voir-coulisses" data-idx="' + idx + '">Voir les coulisses de cette réponse</button>');
            afficherCoulisses(idx);
          }
        });
      }
      historique.push({ role: "user", texte: question + (r.document ? "\n[Photo jointe, données lues : " + JSON.stringify(r.document) + "]" : "") },
                      { role: "assistant", texte: r.texte });
    } catch (err) {
      corps.innerHTML = '<p class="msg-erreur">' + e(err.message) + "</p>";
      etat("");
    } finally {
      bouton.disabled = false;
      champ.focus();
    }
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var q = champ.value.trim();
    if (bouton.disabled || (!q && !image)) return;
    var im = image;
    champ.value = ""; champ.style.height = ""; joindre(null);
    poser(q || "Voici une photo de mon document.", im);
  });
  champ.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); form.requestSubmit(); }
  });
  champ.addEventListener("input", function () { champ.style.height = "auto"; champ.style.height = Math.min(champ.scrollHeight, 160) + "px"; });
  [].forEach.call(document.querySelectorAll(".suggestions button"), function (b) {
    b.addEventListener("click", function () {
      if (bouton.disabled) return;
      if (b.dataset.exemple) {
        fetch(b.dataset.exemple).then(function (x) { return x.blob(); })
          .then(function (blob) { return preparerImage(blob, "bon-de-livraison.jpg"); })
          .then(function (im) { poser("Voici mon bon de livraison : je voudrais retourner un article.", im); })
          .catch(function () { alerte("Exemple indisponible."); });
        return;
      }
      poser(b.textContent.replace(/ /g, " "));
    });
  });
})();
