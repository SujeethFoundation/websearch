
import re
from services.config import settings


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def tokens(value):
    return set(normalize(value).split())


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

    def __init__(self, ontology):
        self.ontology = ontology
        self._catalog = None

    def _load_catalog(self):
        if self._catalog is not None:
            return self._catalog

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

        self._catalog = {
            "ontology": ontology_doc if isinstance(ontology_doc, dict) else {},
            "classes": classes,
            "terminology": terminology,
            "relationships": relationships,
        }
        return self._catalog

    @staticmethod
    def _class_id(item):
        return str(item.get("id") or item.get("name") or "").strip()

    def _term_aliases(self, concept_id, class_item):
        values = [
            class_item.get("name", ""),
            class_item.get("id", ""),
            class_item.get("definition", ""),
        ]
        for field in ("aliases", "synonyms", "alternative_terms", "alternate_names"):
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
        concept_id = self._class_id(class_item)
        query_norm = normalize(query)
        query_tokens = tokens(query)
        aliases = self._term_aliases(concept_id, class_item)
        best = 0.0
        matched = None
        for alias in aliases:
            alias_norm = normalize(alias)
            alias_tokens = tokens(alias)
            if not alias_norm:
                continue
            if alias_norm in query_norm:
                score = 1.0 + min(len(alias_tokens), 4) * 0.1
            else:
                overlap = len(query_tokens & alias_tokens)
                score = overlap / max(1, len(alias_tokens)) * 0.65
                # Do not let a single generic token match a long definition.
                if overlap == 1 and len(alias_tokens) > 2:
                    score *= 0.45
            if score > best:
                best, matched = score, alias
        return best, matched

    def resolve_query(self, query, explicit_concepts=None, explicit_domains=None, purpose="search"):
        catalog = self._load_catalog()
        classes = catalog["classes"]
        if not classes:
            raise RuntimeError(
                "Ontology returned no classes; query-to-concept mapping cannot run safely."
            )

        scores = []
        by_id = {self._class_id(c).lower(): c for c in classes if self._class_id(c)}
        for item in classes:
            cid = self._class_id(item)
            score, matched_term = self._score_class(query, item)
            if score >= 0.28:
                scores.append((score, cid, matched_term))

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
            if not validation.get("valid", False):
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
                    "confidence": round(score_map.get(cid, (1.0, "explicit"))[0], 3),
                    "source": "explicit" if cid in explicit else "query_match",
                }
                for cid in selected
            ],
            "domains": list(dict.fromkeys(domains)),
            "status": "resolved",
            "purpose": purpose,
            "domain_resolution": domain_result,
            "domain_filtering": "concept_selected_domains_only",
        }
