import re
import threading
import time
from services.config import settings


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def tokens(value):
    return set(normalize(value).split())


# Words that carry no topic. A partial match (some of an alias's words appear in the question) counts only the words
# outside this set, so "make a gift" no longer matches "how to make a pyramid" and "tell me about yourself" no longer
# matches "tell me how to make a vodka tonic". A phrase that appears whole in the question still matches as before.
STOPWORDS = frozenset("""
a an the and or but if of to in on at by for from with without into about as is are was were be been am do does did
have has had can could should would will may might must i me my mine we us our you your yours he she it its they them
their this that these those there here what which who whom whose when where why how not no yes so than then too very
just also any some more most other such only own same up out over under again all each both few
make makes made making give gives gave giving tell tells told telling show shows showing get gets got getting take
takes taking let lets need needs want wants like use using go goes going know help helps teach teaches way ways
thing things please tips tip
""".split())


def content_tokens(words):
    return {w for w in words if w not in STOPWORDS}


def get_items(payload, keys):
    """Accept the ontology API's common wrapper shapes without assuming one schema."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # Some endpoints wrap their result in data/items.
        for key in ("data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                for nested in keys:
                    if isinstance(value.get(nested), list):
                        return value[nested]
    return []


class ConceptResolver:
    """
    Maps each natural-language query to ontology class IDs using class names,
    aliases, definitions, preferred terminology and relationship vocabulary.
    It then asks the ontology service to authorize the domain selection.
    """

    def __init__(self, ontology, ttl_seconds=600):
        self.ontology = ontology
        self.ttl = ttl_seconds
        self._catalog = None
        self._loaded_at = None
        self._build_lock = threading.Lock()

    def _load_catalog(self):
        # Reload after the TTL so ontology changes (new aliases, new classes) reach a running instance without a
        # restart. Only the very first load makes a request wait. After that an out-of-date catalog is served as it is
        # while one background thread rebuilds it, so no search waits for three calls to the ontology app. If a rebuild
        # fails, the previous catalog stays in use and the rebuild is tried again in a minute.
        catalog = self._catalog
        if catalog is not None and self._loaded_at is not None and time.monotonic() - self._loaded_at < self.ttl:
            return catalog
        if catalog is None:
            with self._build_lock:  # first load: one builder, the others wait for it
                return self._catalog or self._build_catalog()
        if self._build_lock.acquire(blocking=False):
            def rebuild():
                try:
                    self._build_catalog()
                except Exception:
                    self._loaded_at = time.monotonic() - self.ttl + 60
                finally:
                    self._build_lock.release()
            threading.Thread(target=rebuild, name="ontology-catalog-refresh", daemon=True).start()
        return catalog

    def _build_catalog(self):

        ontology_doc = self.ontology.ontology()
        classes = get_items(ontology_doc, ("classes", "ontology_classes"))
        if not classes:
            classes_payload = self.ontology.classes()
            classes = get_items(classes_payload, ("classes", "ontology_classes"))

        terminology_payload = self.ontology.terminology()
        terminology = get_items(
            terminology_payload, ("terminology", "terms", "preferred_terminology")
        )
        relationships_payload = self.ontology.relationships()
        relationships = get_items(
            relationships_payload, ("relationships", "ontology_relationships")
        )

        # Everything a search or a tagging pass needs from the aliases is worked out here, once per catalog: the
        # normalized text and word set of each alias. Before, every search rebuilt each class's alias list (reading the
        # whole terminology list for each class) and normalized every alias again.
        terms_by_concept = {}
        for term in terminology:
            if not isinstance(term, dict):
                continue
            target = str(term.get("concept_id") or term.get("class_id") or term.get("concept") or term.get("id")).lower()
            values = terms_by_concept.setdefault(target, [])
            for field in ("preferred", "preferred_term", "term", "label"):
                if term.get(field):
                    values.append(term[field])
            for field in ("aliases", "synonyms", "alternative_terms"):
                v = term.get(field, [])
                values.extend([v] if isinstance(v, str) else v if isinstance(v, list) else [])
        query_index, tag_index, by_id = [], {}, {}
        for item in classes:
            cid = self._class_id(item) if isinstance(item, dict) else ""
            if not cid:
                continue
            by_id[cid.lower()] = item
            query_index.append((cid, self._index_aliases(self._query_alias_values(item) + terms_by_concept.get(cid.lower(), []))))
            tag_index[cid.lower()] = self._index_aliases(self._tag_alias_values(item), keep_tokens=True)
        catalog = {
            "ontology": ontology_doc if isinstance(ontology_doc, dict) else {},
            "classes": classes,
            "terminology": terminology,
            "relationships": relationships,
            "query_index": query_index,
            "tag_index": tag_index,
            "by_id": by_id,
        }
        self._catalog, self._loaded_at = catalog, time.monotonic()
        return catalog

    @staticmethod
    def _alias_list(item, fields):
        values = []
        for field in fields:
            value = item.get(field, [])
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, list):
                values.extend(value)
        return values

    def _query_alias_values(self, item):
        return [item.get("name", ""), item.get("id", ""), item.get("definition", "")] + self._alias_list(
            item, ("aliases", "synonyms", "alternative_terms", "alternate_names", "query_phrases"))

    def _tag_alias_values(self, item):
        # tag_document skips query_phrases and the terminology list, so broad intent phrases never tag page content.
        return [item.get("name", ""), str(item.get("id", "")).replace("_", " ")] + self._alias_list(
            item, ("aliases", "synonyms", "alternative_terms", "alternate_names"))

    @staticmethod
    def _index_aliases(values, keep_tokens=True):
        """[(normalized alias, its word set, the alias as written)] without repeats, first spelling kept."""
        seen, out = set(), []
        for value in values:
            if not value:
                continue
            text = str(value)
            norm = normalize(text)
            if norm and norm not in seen:
                seen.add(norm)
                out.append((norm, frozenset(norm.split()), text))
        return out

    @staticmethod
    def _score_alias(padded_query, query_tokens, alias_norm, alias_tokens):
        """How well one alias matches the question. 1.0 or more: the alias appears in the question as whole words (a
        plural s on its last word is accepted). Below 1.0: only some of its words do.

        Two rules keep unrelated questions out. A whole-phrase match is on word boundaries, so "job" is not found in
        "jobbing" nor "sway" in "swaying". A partial match counts topic words only, and one shared word of a longer
        phrase is not a match: "first moon landing" shares one word with "First Job", "monthly car payment" one with
        "monthly gift". A concept that should answer to a single word needs that word as an alias or query phrase."""
        n = len(alias_tokens)
        if f" {alias_norm} " in padded_query or f" {alias_norm}s " in padded_query:
            return 1.0 + min(n, 4) * 0.1
        content = content_tokens(alias_tokens)
        if len(content) < 2:
            # "what the world needs" has one topic word. Sharing it ("the World Series") is not a match; the phrase has
            # to appear whole.
            return 0.0
        overlap = len(query_tokens & content)
        score = overlap / len(content) * 0.65
        if overlap == 1:
            score *= 0.45
        return score

    @classmethod
    def _score_indexed(cls, query_norm, query_tokens, aliases):
        """The best-matching alias of one class: (score, the alias as written). Aliases were normalized in advance."""
        best, matched = 0.0, None
        padded = f" {query_norm} "
        for alias_norm, alias_tokens, alias in aliases:
            score = cls._score_alias(padded, query_tokens, alias_norm, alias_tokens)
            if score > best:
                best, matched = score, alias
        return best, matched

    @staticmethod
    def _class_id(item):
        return str(item.get("id") or item.get("name") or "").strip()

    def _term_aliases(self, concept_id, class_item):
        # query_phrases are matched against questions only. tag_document builds its own list and
        # skips them, so broad intent phrases ("getting into") never tag page content.
        values = [
            class_item.get("name", ""),
            class_item.get("id", ""),
            class_item.get("definition", ""),
        ]
        for field in ("aliases", "synonyms", "alternative_terms", "alternate_names", "query_phrases"):
            value = class_item.get(field, [])
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, list):
                values.extend(value)

        for term in self._catalog["terminology"]:
            if not isinstance(term, dict):
                continue
            target = (
                term.get("concept_id") or term.get("class_id")
                or term.get("concept") or term.get("id")
            )
            if str(target).lower() == concept_id.lower():
                for field in ("preferred", "preferred_term", "term", "label"):
                    if term.get(field):
                        values.append(term[field])
                for field in ("aliases", "synonyms", "alternative_terms"):
                    v = term.get(field, [])
                    values.extend([v] if isinstance(v, str) else v if isinstance(v, list) else [])
        return [str(v) for v in values if v]

    def _score_class(self, query, class_item):
        """Score one class without the prepared index (same rule as _score_indexed)."""
        aliases = self._index_aliases(self._term_aliases(self._class_id(class_item), class_item))
        query_norm = normalize(query)
        return self._score_indexed(query_norm, set(query_norm.split()), aliases)

    EFFECTS = ("expand", "boost", "suppress")

    @staticmethod
    def _behavior(spec, fallback=None):
        """(matched endpoint, effect, weight) from a search_behavior block, or None when it is off or incomplete.
        matched endpoint is "subject", "object" or "either": the end of the relationship the question has to match.
        The effect falls on the concept at the other end."""
        spec = spec if isinstance(spec, dict) else {}
        base = fallback or (None, None, 0.0)
        if spec.get("enabled") is False:
            return None
        matched = str(spec.get("matched_endpoint") or base[0] or "").lower()
        effect = str(spec.get("effect") or base[1] or "").lower()
        try:
            weight = float(spec.get("weight", base[2]))
        except (TypeError, ValueError):
            weight = base[2]
        if matched not in ("subject", "object", "either") or effect not in ConceptResolver.EFFECTS:
            return None
        return matched, effect, max(0.0, min(abs(weight), 1.0))

    def _predicate_behaviors(self, catalog):
        """{predicate: (matched endpoint, effect, weight)} from the ontology's predicates.

        A predicate takes part when its search_behavior block is enabled and names an effect. "illustrates" keeps its
        long-standing behavior when the ontology says nothing more: a match on the object brings in the subject, with no
        ranking weight. With RELATIONSHIP_EFFECTS off, that is the only behavior."""
        behaviors = {"illustrates": ("object", "expand", 0.0)}
        if not settings.relationship_effects:
            return behaviors
        for predicate in (catalog.get("ontology") or {}).get("predicates") or []:
            if not isinstance(predicate, dict) or not predicate.get("id"):
                continue
            pid = str(predicate["id"]).lower()
            spec = predicate.get("search_behavior")
            if spec is None:
                continue
            found = self._behavior(spec, behaviors.get(pid))
            if found:
                behaviors[pid] = found
            else:
                behaviors.pop(pid, None)
        return behaviors

    def _relationship_effects(self, selected, catalog=None):
        """What the ontology's relationships do to a search, given the concepts the question matched.

        Returns [{"concept_id", "effect", "weight", "via"}], one entry per related concept, never one the question itself
        matched. Three effects:
          expand    the related concept joins the search filter, so pages tagged only with it can be returned, and it
                    counts for `weight` in the ranking (0: filter only).
          boost     the filter is unchanged; a page already in the results that also carries the related concept ranks
                    higher by `weight`.
          suppress  the filter is unchanged; a page that carries the related concept ranks lower by `weight`.
        A weight is a share of what carrying everything the question named is worth (1.0). The behavior comes from the
        relationship's predicate (search_behavior in the ontology's predicates); a relationship can carry its own
        search_behavior to override it, or {"enabled": false} to opt out. One hop only. When several relationships reach
        the same concept, it is expanded if any of them expands it, and its weight is the strongest boost less the
        strongest suppress (two relationships saying the same thing do not count twice)."""
        catalog = catalog or self._load_catalog()
        behaviors = self._predicate_behaviors(catalog)
        chosen = {str(c).lower() for c in selected}
        hits = []  # (anchor the question matched, predicate, effect, weight, the concept at the other end, relationship id)
        for rel in catalog.get("relationships") or []:
            if not isinstance(rel, dict):
                continue
            predicate = str(rel.get("predicate", "")).lower()
            behavior = behaviors.get(predicate)
            if "search_behavior" in rel and settings.relationship_effects:
                behavior = self._behavior(rel.get("search_behavior"), behavior)
            if not behavior:
                continue
            matched, effect, weight = behavior
            subject, obj = str(rel.get("subject", "")).strip(), str(rel.get("object", "")).strip()
            via = rel.get("id") or f"{subject} {predicate} {obj}"
            if matched in ("object", "either") and obj.lower() in chosen and subject:
                hits.append((obj.lower(), predicate, effect, weight, subject, via))
            if matched in ("subject", "either") and subject.lower() in chosen and obj:
                hits.append((subject.lower(), predicate, effect, weight, obj, via))
        # The question already chose: "week 3" matches Week 3 and Week. Week would expand to all six weeks, but the
        # question named the one it wants, so that expansion is dropped.
        decided = {(anchor, predicate) for anchor, predicate, effect, _, target, _ in hits
                   if effect == "expand" and target.lower() in chosen}
        found = {}
        for anchor, predicate, effect, weight, target, via in hits:
            if target.lower() in chosen or (effect == "expand" and (anchor, predicate) in decided):
                continue
            entry = found.setdefault(target.lower(), {"concept_id": target, "expand": False, "up": 0.0, "down": 0.0, "via": []})
            entry["expand"] = entry["expand"] or effect == "expand"
            if effect == "suppress":
                entry["down"] = max(entry["down"], weight)
            else:
                entry["up"] = max(entry["up"], weight)
            entry["via"].append(via)
        out = []
        for entry in found.values():
            weight = round(entry["up"] - entry["down"], 3)
            effect = "expand" if entry["expand"] else "boost" if weight > 0 else "suppress" if weight < 0 else None
            if effect:
                out.append({"concept_id": entry["concept_id"], "effect": effect, "weight": weight, "via": entry["via"]})
        return out

    def _illustrating_concepts(self, selected):
        """Concepts that join the search filter through a relationship (see _relationship_effects, "expand")."""
        return [e["concept_id"] for e in self._relationship_effects(selected) if e["effect"] == "expand"]

    def resolve_query(self, query, explicit_concepts=None, explicit_domains=None, purpose="search"):
        catalog = self._load_catalog()
        classes = catalog["classes"]
        if not classes:
            raise RuntimeError(
                "Ontology returned no classes; query-to-concept mapping cannot run safely."
            )

        scores = []
        by_id = catalog["by_id"]
        query_norm = normalize(query)
        query_tokens = set(query_norm.split())
        for cid, aliases in catalog["query_index"]:
            score, matched_term = self._score_indexed(query_norm, query_tokens, aliases)
            if score >= 0.28:
                scores.append((score, cid, matched_term))

        # When the query names a concept outright (a score of 1.0 or more), partial word overlaps
        # such as "Week 2" for the question "week 1" are noise. They would add unrelated concepts
        # and domains to the search, so keep only the strong matches and substantial partial ones.
        if any(score >= 1.0 for score, _, _ in scores):
            scores = [item for item in scores if item[0] >= 0.5]

        explicit = [str(x).strip() for x in (explicit_concepts or []) if str(x).strip()]
        unknown = [x for x in explicit if x.lower() not in by_id]
        if unknown:
            raise ValueError("Unknown ontology concept ID(s): " + ", ".join(unknown))

        # Explicit concept IDs are additive; inferred concepts are still mandatory.
        chosen = {cid.lower(): cid for _, cid, _ in sorted(scores, reverse=True)}
        for cid in explicit:
            chosen[cid.lower()] = cid

        ranked = sorted(
            [(score, cid, term) for score, cid, term in scores],
            key=lambda x: x[0],
            reverse=True,
        )
        max_concepts = max(1, settings.max_query_concepts)
        inferred = [cid for _, cid, _ in ranked[:max_concepts]]
        selected = list(dict.fromkeys(inferred + explicit))

        if not selected:
            return {
                "query": query,
                "ontology_version": catalog["ontology"].get("ontology_version"),
                "matched_concepts": [],
                "concept_matches": [],
                "domains": [],
                "status": "no_match",
                "purpose": purpose,
                "message": "No ontology concepts matched; broad search is intentionally disabled.",
            }

        # Relationships widen the filter (expand) or change the ranking (boost, suppress); see _relationship_effects.
        effects = [e for e in self._relationship_effects(selected, catalog) if e["concept_id"].lower() in by_id]
        related = [e["concept_id"] for e in effects if e["effect"] == "expand"]
        selected = list(dict.fromkeys(selected + related))

        domain_result = self.ontology.resolve_domains(selected, purpose)
        raw_domains = domain_result.get("domains", [])
        domains = []
        for item in raw_domains:
            domain = item.get("domain") if isinstance(item, dict) else item
            if domain:
                domains.append(str(domain).lower())

        # Explicit domain filters must pass ontology authorization.
        explicit_domain_list = [str(x).lower() for x in (explicit_domains or [])]
        if explicit_domain_list:
            validation = self.ontology.validate_domains(explicit_domain_list, purpose)
            # The ontology service answers with "approved" and "rejected" lists and no
            # "valid" flag. Honor a "valid" flag if a future version adds one.
            if "valid" in validation:
                domains_ok = bool(validation["valid"])
            else:
                domains_ok = bool(validation.get("approved")) and not validation.get("rejected")
            if not domains_ok:
                raise PermissionError("One or more requested domains are not approved by ontology.")
            domains = [d for d in domains if d in explicit_domain_list]
            # A user-supplied domain can narrow, never broaden, the concept-selected set.

        score_map = {cid: (score, term) for score, cid, term in ranked}
        return {
            "query": query,
            "ontology_version": (
                domain_result.get("ontology_version")
                or catalog["ontology"].get("ontology_version")
            ),
            "matched_concepts": selected,
            "concept_matches": [
                {
                    "concept_id": cid,
                    "name": by_id.get(cid.lower(), {}).get("name", cid),
                    "matched_term": score_map.get(cid, (None, None))[1],
                    "confidence": round(score_map.get(cid, (1.0, "explicit"))[0], 3) if cid not in related else None,
                    "source": (
                        "explicit" if cid in explicit
                        else "relationship" if cid in related
                        else "query_match"
                    ),
                }
                for cid in selected
            ],
            "relationship_effects": effects,
            "domains": list(dict.fromkeys(domains)),
            "status": "resolved",
            "purpose": purpose,
            "domain_resolution": domain_result,
            "domain_filtering": "concept_selected_domains_only",
        }


    def tag_document(self, title, content, allowed_concepts):
        """
        Assign conservative ontology class IDs to a content chunk.
        The domain's ontology-approved associated_concepts list is the hard boundary.
        Matching uses class names and declared aliases/synonyms, not free-form guessing.
        """
        catalog = self._load_catalog()
        allowed = {str(x).strip().lower() for x in (allowed_concepts or []) if str(x).strip()}
        if not allowed:
            return []

        tag_index = catalog["tag_index"]
        text = normalize(f"{title or ''} {content or ''}")
        text_tokens = set(text.split())
        padded = f" {text} "
        matches = []

        for concept_id in allowed:
            aliases = tag_index.get(concept_id)
            if not aliases:
                continue
            best = 0.0
            for alias_norm, alias_tokens, _ in aliases:
                if len(alias_tokens) == 1:
                    # Single-word labels must occur as a complete token and be reasonably distinctive.
                    score = 1.0 if alias_norm in text_tokens else 0.0
                elif f" {alias_norm} " in padded:
                    score = 1.0
                else:
                    overlap = len(alias_tokens & text_tokens)
                    score = overlap / len(alias_tokens)
                    if overlap < 2:
                        score = 0.0
                best = max(best, score)

            if best >= 0.75:
                matches.append((best, concept_id))

        # Stable order: strongest match first, then canonical ID.
        return [concept_id for _, concept_id in sorted(matches, key=lambda x: (-x[0], x[1]))]
