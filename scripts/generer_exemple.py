"""Génère web/exemples/bon-de-livraison.jpg : la « photo » d'un bon de livraison Pacewell fictif, pour la démonstration
de la lecture de document. Usage : python scripts/generer_exemple.py"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

RACINE = Path(__file__).resolve().parent.parent
F = "/usr/share/fonts/truetype/dejavu/"
gras = lambda t: ImageFont.truetype(F + "DejaVuSans-Bold.ttf", t)
norm = lambda t: ImageFont.truetype(F + "DejaVuSans.ttf", t)

W, H = 1240, 1700
page = Image.new("RGB", (W, H), "#fbfaf6")
d = ImageDraw.Draw(page)
encre, gris, orange = "#10213a", "#5b6678", "#ff5a1f"
d.rectangle([0, 0, W, 14], fill=orange)
d.text((90, 90), "pacewell", font=gras(64), fill=encre)
d.text((92, 168), "Course sur route et trail · 18 rue de la Piste, 59000 Lille", font=norm(24), fill=gris)
d.text((W - 90, 100), "BON DE LIVRAISON", font=gras(34), fill=encre, anchor="ra")
d.line([90, 230, W - 90, 230], fill="#d3d9e0", width=3)
y = 270
for k, v in [("Commande n°", "PW104233"), ("Date de commande", "08/10/2026"), ("Client", "Camille Martin"),
             ("Livraison", "Point relais Mondial Relay"), ("Suivi", "MR48213377FR")]:
    d.text((90, y), k, font=norm(28), fill=gris)
    d.text((520, y), v, font=gras(30) if k == "Commande n°" else norm(30), fill=encre)
    y += 54
y += 40
d.rectangle([90, y, W - 90, y + 60], fill="#e8ebee")
for x, t in [(110, "Article"), (720, "Qté"), (840, "Prix unit."), (W - 110, "Total")]:
    d.text((x, y + 15), t, font=gras(26), fill=encre, anchor="ra" if t == "Total" else "la")
y += 80
for nom, var, q, pu in [("Pacewell Tempo 3", "Pointure 42,5 · bleu nuit", 1, 129.00),
                        ("Chaussettes Pacewell Double Couche", "Taille 39-42", 2, 16.00)]:
    d.text((110, y), nom, font=norm(30), fill=encre)
    d.text((110, y + 40), var, font=norm(24), fill=gris)
    d.text((735, y), str(q), font=norm(30), fill=encre)
    d.text((840, y), f"{pu:.2f} €".replace(".", ","), font=norm(30), fill=encre)
    d.text((W - 110, y), f"{pu * q:.2f} €".replace(".", ","), font=norm(30), fill=encre, anchor="ra")
    y += 110
    d.line([90, y - 20, W - 90, y - 20], fill="#e3e6ea", width=2)
for k, v, f in [("Sous-total", "161,00 €", norm), ("Livraison", "offerte", norm), ("Total TTC", "161,00 €", gras)]:
    d.text((760, y), k, font=f(30), fill=encre)
    d.text((W - 110, y), v, font=f(30), fill=encre, anchor="ra")
    y += 52
y += 60
d.text((90, y), "Retours gratuits sous 30 jours : espace client › Mes commandes › Retourner un article.", font=norm(24), fill=gris)
d.text((90, y + 40), "Service client : aide@pacewell.fr · 03 20 00 00 00", font=norm(24), fill=gris)
d.text((90, H - 90), "Marque fictive de démonstration — document sans valeur.", font=norm(22), fill="#9aa3b0")

# Effet « photo » : feuille légèrement tournée sur un fond de table, lumière inégale, léger flou.
fond = Image.new("RGB", (W + 260, H + 260), "#8a7f72")
ombre = Image.new("L", fond.size, 0)
ImageDraw.Draw(ombre).rectangle([150, 160, 150 + W, 160 + H], fill=110)
ombre = ombre.filter(ImageFilter.GaussianBlur(28))
fond.paste(Image.new("RGB", fond.size, "#3d3630"), (0, 0), ombre)
feuille = page.rotate(-2.2, expand=True, resample=Image.BICUBIC, fillcolor="#8a7f72")
fond.paste(feuille, (110, 100))
lum = Image.linear_gradient("L").resize(fond.size).rotate(35)
fond = Image.composite(fond, Image.new("RGB", fond.size, "#2a2622"), lum.point(lambda v: 205 + v // 5))
fond = fond.filter(ImageFilter.GaussianBlur(0.8)).resize((1000, round(1000 * fond.height / fond.width)))
sortie = RACINE / "web" / "exemples" / "bon-de-livraison.jpg"
fond.save(sortie, quality=82)
print(sortie, fond.size, sortie.stat().st_size // 1024, "Ko")
