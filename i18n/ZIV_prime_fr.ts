<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE TS>
<TS version="2.1" language="fr">
<context>
    <name>Processing</name>
    <message>
        <source>Also produce the contour lines of the terrain</source>
        <translation>Produire aussi les courbes de niveau du terrain</translation>
    </message>
    <message>
        <source>Viewshed: observer height (metres)</source>
        <translation>ZIV : hauteur de l'observateur (en mètres)</translation>
    </message>
    <message>
        <source>Project area (AOI)</source>
        <translation>Sélection de la ZIP</translation>
    </message>
    <message>
        <source>Country / data source</source>
        <translation>Pays / source de données</translation>
    </message>
    <message>
        <source>Analysis radius (metres) - used both for the download buffer and the viewshed</source>
        <translation>Rayon d'analyse (mètres) - utilisé pour le buffer de téléchargement et la ZIV</translation>
    </message>
    <message>
        <source>Enter an analysis radius, in metres: it sets both the area of terrain downloaded and the extent of the viewshed.</source>
        <translation>Saisissez un rayon d'analyse, en mètres : il fixe l'étendue du terrain téléchargé comme celle de la ZIV.</translation>
    </message>
    <message>
        <source>At a radius of %.0f m the viewshed needs about %.1f GB of memory, more than this computer can give it (%.1f GB in total). Reduce the radius.</source>
        <translation>Avec un rayon de %.0f m, la ZIV demande environ %.1f Go de mémoire, plus que ce poste ne peut lui donner (%.1f Go au total). Réduisez le rayon.</translation>
    </message>
    <message>
        <source>A point cloud cannot be used to compute a viewshed: choose the DSM (recommended), the DTM or the DHM.</source>
        <translation>Un nuage de points ne permet pas de calculer une ZIV : choisissez le MNS (recommandé), le MNT ou le MNH.</translation>
    </message>
    <message>
        <source>Keep the tiles merged: the viewshed is computed on a single terrain, and without merging only the first tile would be used.</source>
        <translation>Gardez les dalles fusionnées : la ZIV se calcule sur un seul terrain, et sans fusion seule la première dalle serait prise.</translation>
    </message>
    <message>
        <source>Data type to download</source>
        <translation>Type de données à télécharger</translation>
    </message>
    <message>
        <source>Merge the downloaded tiles (DTM/DSM/DHM)</source>
        <translation>Fusionner les dalles téléchargées (MNT/MNS/MNH)</translation>
    </message>
    <message>
        <source>Load the analysis radius layer into the project</source>
        <translation>Charger la couche du rayon d'analyse dans le projet</translation>
    </message>
    <message>
        <source>Create the visual influence zone from the merged terrain</source>
        <translation>Créer la Zone d'Influence Visuelle (ZIV) à partir du MNT fusionné</translation>
    </message>
    <message>
        <source>Viewshed: symbology</source>
        <translation>ZIV : symbologie</translation>
    </message>
    <message>
        <source>Suggest photo viewpoints (photomontages) on the most visible sectors</source>
        <translation>Suggérer des points de vue photo (photomontages) sur les secteurs les plus visibles</translation>
    </message>
    <message>
        <source>Photo viewpoints: how many to generate (spread near / medium / far)</source>
        <translation>Points de vue photo : nombre à générer (réparti proche / moyen / éloigné)</translation>
    </message>
    <message>
        <source>Viewshed output (raster) - leave empty for a temporary file</source>
        <translation>Sortie ZIV (raster) - laisser vide pour temporaire</translation>
    </message>
    <message>
        <source>Analysis radius output (vector) - leave empty for the working folder</source>
        <translation>Sortie du rayon d'analyse (vecteur) - laisser vide pour le dossier de travail</translation>
    </message>
    <message>
        <source>Download directory</source>
        <translation>Répertoire de téléchargement</translation>
    </message>
    <message>
        <source>Visual influence zone (ZIV)</source>
        <translation>ZIV</translation>
    </message>
    <message>
        <source>Automatic (from the area of interest)</source>
        <translation>Automatique (d'après l'emprise)</translation>
    </message>
    <message>
        <source>United States (CONUS) — USGS 3DEP</source>
        <translation>États-Unis (CONUS) — USGS 3DEP</translation>
    </message>
    <message>
        <source>DTM — Digital Terrain Model (bare earth)</source>
        <translation>MNT — Modèle Numérique de Terrain (sol nu)</translation>
    </message>
    <message>
        <source>DSM — Digital Surface Model (vegetation and buildings included)</source>
        <translation>MNS — Modèle Numérique de Surface (végétation et bâti compris)</translation>
    </message>
    <message>
        <source>DHM — Digital Height Model (objects above ground)</source>
        <translation>MNH — Modèle Numérique de Hauteur (objets au-dessus du sol)</translation>
    </message>
    <message>
        <source>LiDAR — classified point cloud</source>
        <translation>LiDAR — nuage de points classifiés</translation>
    </message>
    <message>
        <source>Night spotlight (default) — hidden in 80% black, visible transparent</source>
        <translation>Spotlight nuit (défaut) — non visible en noir 80 %, visible transparent</translation>
    </message>
    <message>
        <source>Warm gradient — white → yellow → orange → red (v1.3.x style)</source>
        <translation>Dégradé chaud — blanc → jaune → orange → rouge (style v1.3.x)</translation>
    </message>
    <message>
        <source>Translucent red — visible in 30% red, hidden transparent</source>
        <translation>Rouge translucide — visible en rouge 30 %, non visible transparent</translation>
    </message>
    <message>
        <source>Visibility</source>
        <translation>Visibilité</translation>
    </message>
    <message>
        <source>Visibility</source>
        <translation>Visibilité</translation>
    </message>
    <message>
        <source>
ZIV_prime

Downloads the national LiDAR elevation tiles that intersect your project area
and derives the visual influence zone (viewshed) from them.

Countries:
- France: IGN LiDAR HD (DTM / DSM / DHM / raw point cloud).
- Contiguous United States: USGS 3DEP for the terrain, LANDFIRE for vegetation
  height, USA Structures for buildings, TIGER/Line for roads.
- The country is deduced from the area of interest; the "Country" field forces
  it, which a saved model should do so that it states its source plainly.

Data types:
- DTM: bare earth.
- DSM: vegetation and buildings included.
- DHM: height of the objects above the ground.
- LiDAR: raw classified point cloud.

Analysis radius (m):
- One value, used both as the buffer around the area of interest for the
  download AND as the analysis radius of the viewshed.
- The buffer is written as a layer and drawn with a red dashed outline.
- "Analysis radius output" says where to write it; left empty it follows the
  working folder, which is temporary when the viewshed output is.

Create the viewshed:
- Runs the embedded "Creation ZIV" processing model over the merged terrain.
  The visibility algorithms ship with the plugin: no companion plugin needed.
- The output raster is styled as a night effect: hidden areas (0 and NoData)
  are painted 80% black, while the project footprint (0.1) and the genuinely
  visible cells (1 and above) stay transparent over the basemap.
- Masking: cells falling under woodland or on a building are forced out of
  visibility, because nobody stands on a canopy or a roof. France draws those
  footprints from BD Foret and BDTOPO, the United States from LANDFIRE and USA
  Structures. If a service is unavailable the viewshed comes out unmasked
  rather than not at all.

Where no 1-metre LiDAR covers the area, the terrain is served at about 10 m and
the working resolution is raised to match, rather than interpolated finer than
the data really is. The log says so.
</source>
        <translation>
ZIV_prime

Télécharge les dalles LiDAR nationales qui intersectent votre emprise projet
et en dérive la Zone d'Influence Visuelle (ZIV).

Pays :
- France : IGN LiDAR HD (MNT / MNS / MNH / nuage de points brut).
- États-Unis continentaux : USGS 3DEP pour le terrain, LANDFIRE pour la
  hauteur de végétation, USA Structures pour le bâti, TIGER/Line pour les
  routes.
- Le pays est déduit de l'emprise ; le champ « Pays » l'impose, ce qu'un
  modèle enregistré a intérêt à faire pour énoncer sa source noir sur blanc.

Types de données :
- MNT : sol nu.
- MNS : végétation et bâti compris.
- MNH : hauteur des objets au-dessus du sol.
- LiDAR : nuage de points classifiés brut.

Rayon d'analyse (m) :
- Une seule valeur, servant à la fois de tampon autour de l'emprise pour le
  téléchargement ET de rayon d'analyse pour la ZIV.
- Le tampon est écrit comme une couche et affiché avec un contour rouge en
  pointillés.
- « Sortie du rayon d'analyse » fixe où l'écrire ; laissée vide, elle suit le
  dossier de travail, temporaire quand la sortie ZIV l'est aussi.

Créer la ZIV :
- Exécute le modèle « Création ZIV » embarqué sur le terrain fusionné. Les
  algorithmes de visibilité sont livrés avec le greffon : aucune dépendance.
- Le raster de sortie est stylisé en effet nuit : les zones non visibles
  (0 et NoData) sont peintes en noir à 80 %, tandis que l'empreinte projet
  (0.1) et les cellules réellement visibles (1 et plus) restent transparentes
  au-dessus du fond de carte.
- Masquage : les cellules tombant sous un couvert boisé ou sur un bâtiment
  sont forcées hors visibilité, car personne ne se tient sur une canopée ni
  sur un toit. La France tire ces empreintes de la BD Forêt et de la BDTOPO,
  les États-Unis de LANDFIRE et d'USA Structures. Si un service est
  indisponible, la ZIV sort non masquée plutôt que pas du tout.

Là où aucun LiDAR à 1 m ne couvre l'emprise, le terrain est servi à environ
10 m et la résolution de travail est relevée d'autant, plutôt qu'interpolée
plus fine que la donnée ne l'est. Le journal le dit.
</translation>
    </message>
    <message>
        <source>&lt;p&gt;Prepares an observer point layer for the visibility analysis: each point is reprojected into the coordinate system of the terrain model, &lt;b&gt;snapped to the centre of the cell&lt;/b&gt; that contains it, and given its analysis parameters.&lt;/p&gt;&lt;p&gt;For every parameter, a field of the layer wins over the typed value: that is how masts of differing heights are handled in one go.&lt;/p&gt;&lt;p&gt;Points falling outside the terrain are dropped and reported.&lt;/p&gt;</source>
        <translation>&lt;p&gt;Prépare une couche de points d'observation pour l'analyse de visibilité : chaque point est reprojeté dans le système du modèle numérique de terrain, &lt;b&gt;calé au centre de la cellule&lt;/b&gt; qui le contient, et reçoit ses paramètres d'analyse.&lt;/p&gt;&lt;p&gt;Pour chaque paramètre, un champ de la couche l'emporte sur la valeur saisie : c'est ainsi qu'on traite d'un coup des mâts de hauteurs différentes.&lt;/p&gt;&lt;p&gt;Les points hors emprise du terrain sont écartés et signalés.&lt;/p&gt;</translation>
    </message>
    <message>
        <source>&lt;p&gt;Produces a visibility map from the observer points, over a digital terrain model.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;&lt;b&gt;Binary viewshed&lt;/b&gt; — 1 where the cell is seen, 0 otherwise.&lt;/li&gt;&lt;li&gt;&lt;b&gt;Depth below horizon&lt;/b&gt; — 0 where the target is seen, otherwise how many &lt;b&gt;metres&lt;/b&gt; would have to be added for it to appear.&lt;/li&gt;&lt;li&gt;&lt;b&gt;Horizon&lt;/b&gt; — the ridge cells that close the landscape.&lt;/li&gt;&lt;/ul&gt;&lt;p&gt;With several points, &lt;i&gt;Addition&lt;/i&gt; counts how many of them see each cell: that is the visual exposure map the viewshed chain styles.&lt;/p&gt;&lt;p&gt;The point layer must carry the fields &lt;code&gt;observ_hgt&lt;/code&gt; and &lt;code&gt;radius&lt;/code&gt;, which Create viewpoints produces. The optional fields &lt;code&gt;targ_hgt&lt;/code&gt;, &lt;code&gt;radius_in&lt;/code&gt;, &lt;code&gt;azim_1&lt;/code&gt;, &lt;code&gt;azim_2&lt;/code&gt;, &lt;code&gt;angle_up&lt;/code&gt; and &lt;code&gt;angle_down&lt;/code&gt; are honoured when present.&lt;/p&gt;</source>
        <translation>&lt;p&gt;Produit une carte de visibilité depuis les points d'observation, sur un modèle numérique de terrain.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;&lt;b&gt;Binary viewshed&lt;/b&gt; — 1 si la cellule est vue, 0 sinon.&lt;/li&gt;&lt;li&gt;&lt;b&gt;Depth below horizon&lt;/b&gt; — 0 là où la cible est vue, sinon le nombre de &lt;b&gt;mètres&lt;/b&gt; qu'il faudrait lui ajouter pour qu'elle apparaisse.&lt;/li&gt;&lt;li&gt;&lt;b&gt;Horizon&lt;/b&gt; — les cellules de crête qui ferment le paysage.&lt;/li&gt;&lt;/ul&gt;&lt;p&gt;Avec plusieurs points, &lt;i&gt;Addition&lt;/i&gt; compte combien de points voient chaque cellule : c'est la carte d'exposition visuelle que la chaîne ZIV stylise.&lt;/p&gt;&lt;p&gt;La couche de points doit porter les champs &lt;code&gt;observ_hgt&lt;/code&gt; et &lt;code&gt;radius&lt;/code&gt;, que « Create viewpoints » produit. Les champs facultatifs &lt;code&gt;targ_hgt&lt;/code&gt;, &lt;code&gt;radius_in&lt;/code&gt;, &lt;code&gt;azim_1&lt;/code&gt;, &lt;code&gt;azim_2&lt;/code&gt;, &lt;code&gt;angle_up&lt;/code&gt; et &lt;code&gt;angle_down&lt;/code&gt; sont pris en compte s'ils existent.&lt;/p&gt;</translation>
    </message>
</context>
<context>
    <name>ZIV_prime</name>
    <message>
        <source>Language / Langue :</source>
        <translation>Langue / Language :</translation>
    </message>
    <message>
        <source>or draw it:</source>
        <translation>ou tracez-la :</translation>
    </message>
    <message>
        <source>Draw an area</source>
        <translation>Tracer une emprise</translation>
    </message>
    <message>
        <source>Draw a viewpoint</source>
        <translation>Tracer un point</translation>
    </message>
    <message>
        <source>Click to add a vertex, double-click to finish, Esc to clear. The area is written to a temporary layer of the project.</source>
        <translation>Un clic par sommet, double-clic pour terminer, Échap pour effacer. L'emprise est écrite dans une couche temporaire du projet.</translation>
    </message>
    <message>
        <source>One click is enough. The point stands for a single observer carrying an object of the height set below.</source>
        <translation>Un seul clic suffit. Le point vaut un observateur unique portant un objet de la hauteur indiquée ci-dessous.</translation>
    </message>
    <message>
        <source>Emprise tracee</source>
        <translation>Emprise tracée</translation>
    </message>
    <message>
        <source>Observateur trace</source>
        <translation>Observateur tracé</translation>
    </message>
    <message>
        <source>Emprise abandonnee : il faut au moins trois sommets distincts pour delimiter une surface.</source>
        <translation>Emprise abandonnée : il faut au moins trois sommets distincts pour délimiter une surface.</translation>
    </message>
    <message>
        <source>Emprise abandonnee : le contour se recoupe et n'a pas pu etre repare.</source>
        <translation>Emprise abandonnée : le contour se recoupe et n'a pas pu être réparé.</translation>
    </message>
    <message>
        <source>Trace enregistre, mais le formulaire n'a pas pu etre remis au premier plan (%s).</source>
        <translation>Tracé enregistré, mais le formulaire n'a pas pu être remis au premier plan (%s).</translation>
    </message>
    <message>
        <source>Zones d'influence visuelle (ZIV): downloads national LiDAR tiles and computes visibility</source>
        <translation>Zones d'influence visuelle (ZIV) : téléchargement des dalles LiDAR HD de l'IGN et calcul de la visibilité</translation>
    </message>
    <message>
        <source>⚙ Preferences…</source>
        <translation>⚙ Préférences…</translation>
    </message>
    <message>
        <source>Configure the plugin defaults: viewshed symbology and log folder.</source>
        <translation>Configurer les défauts du greffon : symbologie ZIV et dossier de logs.</translation>
    </message>
</context>
</TS>
