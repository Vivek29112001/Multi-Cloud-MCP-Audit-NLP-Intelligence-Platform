# from __future__ import annotations

# import re
# from collections import defaultdict
# from difflib import SequenceMatcher
# from typing import Any, Iterable

# from app.providers.aws.models import (
#     AWSBillingComponent,
#     AWSBillingDiscoveryResult,
#     AWSClassifiedRegion,
#     AWSClassifiedService,
#     AWSInfrastructureClassificationResult,
#     AWSRegionDiscoveryResult,
#     AWSResource,
#     AWSResourceDiscoveryResult,
#     AWSResourceRelationship,
# )


# class AWSInfrastructureClassificationService:
#     COST_EPSILON = 1e-9

#     def classify(self, *, regions: AWSRegionDiscoveryResult, resources: AWSResourceDiscoveryResult, billing: AWSBillingDiscoveryResult) -> AWSInfrastructureClassificationResult:
#         relationships = self._discover_relationships(resources.resources)
#         service_result = self._classify_services(resources=resources, billing=billing, relationships=relationships)
#         region_result = self._classify_regions(regions=regions, resources=resources, billing=billing)
#         warnings = list(billing.warnings)
#         return AWSInfrastructureClassificationResult(
#             billing_available=billing.available,
#             primary_paid_services=service_result["primary"],
#             zero_cost_services=service_result["zero_cost"],
#             supporting_services=service_result["supporting"],
#             discovered_unbilled_services=service_result["unbilled"],
#             billing_only_services=service_result["billing_only"],
#             non_positive_billing_services=service_result["non_positive"],
#             paid_regions=region_result["paid"],
#             used_unbilled_regions=region_result["used_unbilled"],
#             enabled_unused_regions=region_result["enabled_unused"],
#             billed_only_regions=region_result["billed_only"],
#             relationships=relationships,
#             warnings=warnings,
#         )

#     def _classify_services(self, *, resources: AWSResourceDiscoveryResult, billing: AWSBillingDiscoveryResult, relationships: list[AWSResourceRelationship]):
#         # Missing billing evidence is UNKNOWN, not UNBILLED. Do not manufacture
#         # paid/zero-cost/unbilled classifications when the Billing MCP failed.
#         if not billing.available:
#             return {
#                 "primary": [],
#                 "zero_cost": [],
#                 "supporting": [],
#                 "unbilled": [],
#                 "billing_only": [],
#                 "non_positive": [],
#             }

#         discovered = {x.service.strip().lower(): x for x in resources.detected_services if x.service.strip()}
#         billing_matches = {x.billing_service: self._best_service_match(billing_name=x.billing_service, discovered_services=discovered.keys()) for x in billing.service_costs}
#         matched = defaultdict(list); unmatched=[]
#         for cost in billing.service_costs:
#             match=billing_matches.get(cost.billing_service)
#             (matched[match[0]] if match else unmatched).append(cost)
#         components_by_billing=defaultdict(list)
#         for c in billing.components: components_by_billing[c.billing_service].append(c)

#         paid_discovered=set()
#         for service,costs in matched.items():
#             if sum(x.amount for x in costs)>self.COST_EPSILON: paid_discovered.add(service)

#         adjacency=defaultdict(set)
#         for edge in relationships:
#             a=(edge.source_service or "").lower().strip(); b=(edge.target_service or "").lower().strip()
#             if a and b and a!=b: adjacency[a].add(b); adjacency[b].add(a)

#         primary=[]; zero_cost=[]; supporting=[]; unbilled=[]; billing_only=[]; non_positive=[]
#         for service, entry in discovered.items():
#             costs=matched.get(service,[]); amount=sum(x.amount for x in costs)
#             names=sorted({x.billing_service for x in costs})
#             comps=self._components_for_names(names,components_by_billing)
#             methods=[]; confidences=[]
#             for name in names:
#                 m=billing_matches.get(name)
#                 if m: methods.append(m[1]); confidences.append(m[2])
#             related=sorted(x for x in adjacency.get(service,set()) if x in paid_discovered)
#             has_billing=bool(costs)
#             if amount>self.COST_EPSILON:
#                 category="PRIMARY_PAID"; financial="PAID"
#             elif has_billing and abs(amount)<=self.COST_EPSILON:
#                 category="ZERO_COST_OR_FREE_TIER_CANDIDATE"; financial="ZERO_COST_BILLING_EVIDENCE"
#             elif has_billing and amount < -self.COST_EPSILON:
#                 category="BILLING_CREDIT_OR_ADJUSTMENT"; financial="CREDIT_OR_ADJUSTMENT"
#             elif related:
#                 category="SUPPORTING_RELATED"; financial="NO_BILLING_RECORD"
#             else:
#                 category="DISCOVERED_UNBILLED"; financial="NO_BILLING_RECORD"
#             item=AWSClassifiedService(key=service,category=category,paid=amount>self.COST_EPSILON,financial_status=financial,billing_amount=amount,currency=billing.currency,billing_services=names,billing_components=comps,discovered_service=service,resource_count=entry.resource_count,regions=entry.regions,related_primary_services=related,billing_match_method=(",".join(sorted(set(methods))) if methods else None),billing_match_confidence=(max(confidences) if confidences else None))
#             if category=="PRIMARY_PAID": primary.append(item)
#             elif category=="ZERO_COST_OR_FREE_TIER_CANDIDATE": zero_cost.append(item)
#             elif category=="SUPPORTING_RELATED": supporting.append(item)
#             elif category=="BILLING_CREDIT_OR_ADJUSTMENT": non_positive.append(item)
#             else: unbilled.append(item)

#         for cost in unmatched:
#             positive=cost.amount>self.COST_EPSILON; zero=abs(cost.amount)<=self.COST_EPSILON
#             item=AWSClassifiedService(
#                 key=f"billing:{self._normalize(cost.billing_service)}",
#                 category=("PRIMARY_PAID_BILLING_ONLY" if positive else ("ZERO_COST_BILLING_ONLY" if zero else "BILLING_CREDIT_OR_ADJUSTMENT_ONLY")),
#                 paid=positive,
#                 financial_status=("PAID" if positive else ("ZERO_COST_BILLING_EVIDENCE" if zero else "CREDIT_OR_ADJUSTMENT")),
#                 billing_amount=cost.amount,
#                 currency=cost.unit or billing.currency,
#                 billing_services=[cost.billing_service],
#                 billing_components=self._components_for_names([cost.billing_service],components_by_billing),
#             )
#             (billing_only if positive else non_positive).append(item)

#         primary.sort(key=lambda x:(-x.billing_amount,x.key)); zero_cost.sort(key=lambda x:(-x.resource_count,x.key)); supporting.sort(key=lambda x:(-x.resource_count,x.key)); unbilled.sort(key=lambda x:(-x.resource_count,x.key)); billing_only.sort(key=lambda x:(-x.billing_amount,x.key)); non_positive.sort(key=lambda x:(x.billing_amount,x.key))
#         return {"primary":primary,"zero_cost":zero_cost,"supporting":supporting,"unbilled":unbilled,"billing_only":billing_only,"non_positive":non_positive}

#     def _classify_regions(self, *, regions, resources, billing):
#         if not billing.available:
#             return {"paid": [], "used_unbilled": [], "enabled_unused": [], "billed_only": []}

#         enabled={x.region_name for x in regions.regions if x.enabled and x.region_name}; counts=defaultdict(int)
#         for r in resources.resources:
#             region=(r.region or "").strip()
#             if region and region.lower()!="global": counts[region]+=1
#         costs={x.region:x for x in billing.region_costs if x.region}; result={"paid":[],"used_unbilled":[],"enabled_unused":[],"billed_only":[]}
#         for region in sorted(enabled|set(counts)|set(costs)):
#             ce=costs.get(region); amount=ce.amount if ce else 0.0; unit=ce.unit if ce else billing.currency; count=counts.get(region,0); en=region in enabled
#             if amount>self.COST_EPSILON: category="PAID_PRIMARY" if en or count else "PAID_BILLING_ONLY"; bucket="paid"
#             elif count: category="USED_UNBILLED"; bucket="used_unbilled"
#             elif en: category="ENABLED_UNUSED"; bucket="enabled_unused"
#             else: category="BILLING_ONLY_NON_POSITIVE"; bucket="billed_only"
#             result[bucket].append(AWSClassifiedRegion(region=region,category=category,billing_amount=amount,currency=unit,resource_count=count,enabled=en))
#         for k in result: result[k].sort(key=lambda x:x.region)
#         result["paid"].sort(key=lambda x:(-x.billing_amount,x.region))
#         return result

#     @staticmethod
#     def _components_for_names(names: Iterable[str], mapping):
#         out=[c for n in names for c in mapping.get(n,[])]; out.sort(key=lambda x:(-x.amount,x.usage_type)); return out

#     def _best_service_match(self, *, billing_name: str, discovered_services: Iterable[str]):
#         billing_norm=self._normalize(billing_name); aliases=self._dynamic_aliases(billing_name); best=None
#         for service in discovered_services:
#             norm=self._normalize(service)
#             if not norm: continue
#             if norm in aliases: cand=(service,"dynamic_alias",1.0)
#             elif len(norm)>=3 and (norm in billing_norm or billing_norm in norm): cand=(service,"normalized_substring",0.92)
#             else:
#                 ratio=SequenceMatcher(None,norm,billing_norm).ratio()
#                 if ratio<0.82: continue
#                 cand=(service,"string_similarity",round(ratio,4))
#             if best is None or cand[2]>best[2]: best=cand
#         return best

#     @classmethod
#     def _dynamic_aliases(cls,value):
#         tokens=cls._words(value); aliases={cls._normalize(value)}
#         for t in tokens: aliases.add(cls._normalize(t))
#         for width in range(2,min(len(tokens),7)+1):
#             for start in range(0,len(tokens)-width+1):
#                 window=tokens[start:start+width]; aliases.add(cls._normalize("".join(window))); aliases.add(cls._compress_initials(window))
#         return {x for x in aliases if x}
#     @staticmethod
#     def _words(value):
#         expanded=re.sub(r"([a-z0-9])([A-Z])",r"\1 \2",value); expanded=re.sub(r"([A-Z]+)([A-Z][a-z])",r"\1 \2",expanded); return re.findall(r"[A-Za-z]+\d*|\d+",expanded)
#     @classmethod
#     def _compress_initials(cls,words):
#         initials=[cls._normalize(w)[0] for w in words if cls._normalize(w)]; out=[]; i=0
#         while i<len(initials):
#             c=initials[i]; j=i+1
#             while j<len(initials) and initials[j]==c:j+=1
#             out.append(c); count=j-i
#             if count>1:out.append(str(count))
#             i=j
#         return "".join(out)
#     @staticmethod
#     def _normalize(value): return re.sub(r"[^a-z0-9]+","",value.lower())

#     def _discover_relationships(self, resources: list[AWSResource]):
#         ids=defaultdict(list)
#         for r in resources:
#             for identifier in (r.arn,r.resource_id):
#                 n=(identifier or "").strip().lower()
#                 if len(n)>=6: ids[n].append(r)
#         out=[]; seen=set()
#         for source in resources:
#             sk=self._resource_key(source)
#             if not sk:continue
#             for scalar in self._flatten_scalars(source.properties):
#                 for candidate in self._reference_candidates(scalar):
#                     for target in ids.get(candidate.lower(),[]):
#                         tk=self._resource_key(target)
#                         if not tk or tk==sk:continue
#                         pair=tuple(sorted((sk,tk)))
#                         if pair in seen:continue
#                         seen.add(pair); out.append(AWSResourceRelationship(source_arn=source.arn,source_resource_id=source.resource_id,source_service=source.service,target_arn=target.arn,target_resource_id=target.resource_id,target_service=target.service,evidence=candidate))
#         return out
#     @classmethod
#     def _flatten_scalars(cls,v):
#         out=[]
#         if isinstance(v,dict):
#             for c in v.values():out.extend(cls._flatten_scalars(c))
#         elif isinstance(v,(list,tuple,set)):
#             for c in v:out.extend(cls._flatten_scalars(c))
#         elif v is not None:out.append(str(v))
#         return out
#     @staticmethod
#     def _reference_candidates(v): return {m.rstrip(".,;:)") for m in re.findall(r"arn:[^\s\"',}\]]+|[A-Za-z0-9][A-Za-z0-9._:/-]{5,}",v) if len(m.rstrip(".,;:)"))>=6}
#     @staticmethod
#     def _resource_key(r):
#         if r.arn:return r.arn.lower()
#         if r.resource_id:return f"{(r.service or '').lower()}|{(r.region or '').lower()}|{r.resource_id.lower()}"
#         return None



from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Iterable

from app.providers.aws.models import (
    AWSBillingDiscoveryResult,
    AWSClassifiedRegion,
    AWSClassifiedService,
    AWSInfrastructureClassificationResult,
    AWSRegionDiscoveryResult,
    AWSResource,
    AWSResourceDiscoveryResult,
    AWSResourceRelationship,
)


class AWSInfrastructureClassificationService:
    COST_EPSILON = 1e-9

    def classify(
        self,
        *,
        regions: AWSRegionDiscoveryResult,
        resources: AWSResourceDiscoveryResult,
        billing: AWSBillingDiscoveryResult,
    ) -> AWSInfrastructureClassificationResult:
        relationships = self._discover_relationships(resources.resources)

        service_result = self._classify_services(
            resources=resources,
            billing=billing,
            relationships=relationships,
        )
        region_result = self._classify_regions(
            regions=regions,
            resources=resources,
            billing=billing,
        )

        return AWSInfrastructureClassificationResult(
            billing_available=billing.available,

            # Final three service buckets.
            primary_services=service_result["primary"],
            secondary_services=service_result["secondary"],
            unclassified_services=service_result["unclassified"],

            # Legacy fields retained so saved-snapshot/NLP code does not break.
            # Primary includes both positive-cost and zero-cost billing evidence.
            primary_paid_services=[
                item
                for item in service_result["primary"]
                if item.financial_status == "PAID"
            ],
            zero_cost_services=[
                item
                for item in service_result["primary"]
                if item.financial_status == "ZERO_COST_BILLING_EVIDENCE"
            ],
            supporting_services=[],
            discovered_unbilled_services=service_result["secondary"],
            billing_only_services=service_result["billing_only"],
            non_positive_billing_services=service_result["non_positive"],

            paid_regions=region_result["paid"],
            used_unbilled_regions=region_result["used_unbilled"],
            enabled_unused_regions=region_result["enabled_unused"],
            billed_only_regions=region_result["billing_only"],
            billing_only_locations=region_result["billing_only"],

            relationships=relationships,
            warnings=list(billing.warnings),
        )

    def _classify_services(
        self,
        *,
        resources: AWSResourceDiscoveryResult,
        billing: AWSBillingDiscoveryResult,
        relationships: list[AWSResourceRelationship],
    ) -> dict[str, list[AWSClassifiedService]]:
        discovered = {
            entry.service.strip().lower(): entry
            for entry in resources.detected_services
            if entry.service and entry.service.strip()
        }

        # Billing unavailable means "unknown", not "unbilled".
        if not billing.available:
            unclassified = [
                AWSClassifiedService(
                    key=service,
                    category="UNCLASSIFIED",
                    paid=False,
                    financial_status="BILLING_UNAVAILABLE",
                    currency=billing.currency,
                    discovered_service=service,
                    resource_count=entry.resource_count,
                    regions=entry.regions,
                    classification_reason=(
                        "Service was discovered, but billing evidence is unavailable."
                    ),
                )
                for service, entry in discovered.items()
            ]
            unclassified.sort(key=lambda x: (-x.resource_count, x.key))
            return {
                "primary": [],
                "secondary": [],
                "unclassified": unclassified,
                "billing_only": [],
                "non_positive": [],
            }

        billing_matches = {
            cost.billing_service: self._best_service_match(
                billing_name=cost.billing_service,
                discovered_services=discovered.keys(),
            )
            for cost in billing.service_costs
        }

        matched: dict[str, list[Any]] = defaultdict(list)
        unmatched: list[Any] = []
        for cost in billing.service_costs:
            match = billing_matches.get(cost.billing_service)
            if match:
                matched[match[0]].append(cost)
            else:
                unmatched.append(cost)

        components_by_billing: dict[str, list[Any]] = defaultdict(list)
        for component in billing.components:
            components_by_billing[component.billing_service].append(component)

        adjacency: dict[str, set[str]] = defaultdict(set)
        for edge in relationships:
            source = (edge.source_service or "").lower().strip()
            target = (edge.target_service or "").lower().strip()
            if source and target and source != target:
                adjacency[source].add(target)
                adjacency[target].add(source)

        primary: list[AWSClassifiedService] = []
        secondary: list[AWSClassifiedService] = []
        unclassified: list[AWSClassifiedService] = []
        billing_only: list[AWSClassifiedService] = []
        non_positive: list[AWSClassifiedService] = []

        for service, entry in discovered.items():
            costs = matched.get(service, [])
            amount = sum(cost.amount for cost in costs)
            billing_names = sorted({cost.billing_service for cost in costs})
            components = self._components_for_names(
                billing_names,
                components_by_billing,
            )

            methods: list[str] = []
            confidences: list[float] = []
            for name in billing_names:
                match = billing_matches.get(name)
                if match:
                    methods.append(match[1])
                    confidences.append(match[2])

            confidence = max(confidences) if confidences else None
            match_method = ",".join(sorted(set(methods))) if methods else None
            has_billing_record = bool(costs)

            if amount > self.COST_EPSILON:
                financial_status = "PAID"
            elif has_billing_record and abs(amount) <= self.COST_EPSILON:
                financial_status = "ZERO_COST_BILLING_EVIDENCE"
            elif has_billing_record and amount < -self.COST_EPSILON:
                financial_status = "CREDIT_OR_ADJUSTMENT"
            else:
                financial_status = "NO_BILLING_RECORD"

            # Final agreed rule:
            # discovered + billing match confidence + PAID/ZERO_COST evidence
            # => PRIMARY. Region is intentionally NOT a condition.
            is_primary = (
                bool(service)
                and confidence is not None
                and financial_status
                in {"PAID", "ZERO_COST_BILLING_EVIDENCE"}
            )

            related_services = sorted(adjacency.get(service, set()))

            if is_primary:
                category = "PRIMARY"
                reason = (
                    "Discovered service with matched billing evidence "
                    f"({financial_status}) and billing match confidence "
                    f"{confidence:.4f}."
                )
            elif financial_status in {
                "NO_BILLING_RECORD",
                "CREDIT_OR_ADJUSTMENT",
            }:
                category = "SECONDARY"
                reason = (
                    "Service was discovered, but it does not have qualifying "
                    "PAID or ZERO_COST_BILLING_EVIDENCE for Primary."
                )
            else:
                category = "UNCLASSIFIED"
                reason = (
                    "Available evidence is insufficient for deterministic "
                    "Primary or Secondary classification."
                )

            item = AWSClassifiedService(
                key=service,
                category=category,
                paid=financial_status == "PAID",
                financial_status=financial_status,
                billing_amount=amount,
                currency=billing.currency,
                billing_services=billing_names,
                billing_components=components,
                discovered_service=service,
                resource_count=entry.resource_count,
                regions=entry.regions,
                related_primary_services=related_services,
                billing_match_method=match_method,
                billing_match_confidence=confidence,
                classification_reason=reason,
            )

            if category == "PRIMARY":
                primary.append(item)
            elif category == "SECONDARY":
                secondary.append(item)
            else:
                unclassified.append(item)

        # Billing records without reliable discovery correlation must not become
        # Primary. Keep them separately as billing-only/unclassified evidence.
        for cost in unmatched:
            if cost.amount > self.COST_EPSILON:
                financial_status = "PAID"
            elif abs(cost.amount) <= self.COST_EPSILON:
                financial_status = "ZERO_COST_BILLING_EVIDENCE"
            else:
                financial_status = "CREDIT_OR_ADJUSTMENT"

            item = AWSClassifiedService(
                key=f"billing:{self._normalize(cost.billing_service)}",
                category="UNCLASSIFIED",
                paid=financial_status == "PAID",
                financial_status=financial_status,
                billing_amount=cost.amount,
                currency=cost.unit or billing.currency,
                billing_services=[cost.billing_service],
                billing_components=self._components_for_names(
                    [cost.billing_service],
                    components_by_billing,
                ),
                classification_reason=(
                    "Billing evidence exists, but no reliable discovered-service "
                    "match was found."
                ),
            )
            billing_only.append(item)
            unclassified.append(item)
            if financial_status == "CREDIT_OR_ADJUSTMENT":
                non_positive.append(item)

        primary.sort(key=lambda x: (-x.billing_amount, -x.resource_count, x.key))
        secondary.sort(key=lambda x: (-x.resource_count, x.key))
        unclassified.sort(key=lambda x: (-x.resource_count, x.key))
        billing_only.sort(key=lambda x: (-x.billing_amount, x.key))
        non_positive.sort(key=lambda x: (x.billing_amount, x.key))

        return {
            "primary": primary,
            "secondary": secondary,
            "unclassified": unclassified,
            "billing_only": billing_only,
            "non_positive": non_positive,
        }

    def _classify_regions(
        self,
        *,
        regions: AWSRegionDiscoveryResult,
        resources: AWSResourceDiscoveryResult,
        billing: AWSBillingDiscoveryResult,
    ) -> dict[str, list[AWSClassifiedRegion]]:
        # Runtime region catalogue from DescribeRegions. No static AWS region list.
        known_regions = {
            entry.region_name
            for entry in regions.regions
            if entry.region_name
        }
        enabled_regions = {
            entry.region_name
            for entry in regions.regions
            if entry.enabled and entry.region_name
        }

        resource_counts: dict[str, int] = defaultdict(int)
        for resource in resources.resources:
            region = (resource.region or "").strip()
            if region and region.lower() != "global":
                resource_counts[region] += 1

        billing_costs = {
            item.region: item
            for item in billing.region_costs
            if item.region
        }

        result: dict[str, list[AWSClassifiedRegion]] = {
            "paid": [],
            "used_unbilled": [],
            "enabled_unused": [],
            "billing_only": [],
        }

        # Infrastructure-region classification is driven by dynamically
        # discovered AWS region identifiers.
        for region in sorted(known_regions | set(resource_counts)):
            cost = billing_costs.get(region)
            amount = cost.amount if cost else 0.0
            currency = cost.unit if cost else billing.currency
            resource_count = resource_counts.get(region, 0)
            enabled = region in enabled_regions

            if billing.available and amount > self.COST_EPSILON:
                result["paid"].append(
                    AWSClassifiedRegion(
                        region=region,
                        category="PAID_REGION",
                        billing_amount=amount,
                        currency=currency,
                        resource_count=resource_count,
                        enabled=enabled,
                    )
                )
            elif resource_count:
                result["used_unbilled"].append(
                    AWSClassifiedRegion(
                        region=region,
                        category="USED_WITHOUT_POSITIVE_REGIONAL_BILLING",
                        billing_amount=amount,
                        currency=currency,
                        resource_count=resource_count,
                        enabled=enabled,
                    )
                )
            elif enabled:
                result["enabled_unused"].append(
                    AWSClassifiedRegion(
                        region=region,
                        category="ENABLED_UNUSED",
                        billing_amount=amount,
                        currency=currency,
                        resource_count=0,
                        enabled=True,
                    )
                )

        # Cost Explorer locations that are not in DescribeRegions are financial
        # evidence only. They are NOT counted as AWS infrastructure Paid Regions.
        for region, cost in billing_costs.items():
            if region in known_regions:
                continue
            result["billing_only"].append(
                AWSClassifiedRegion(
                    region=region,
                    category="BILLING_ONLY_LOCATION",
                    billing_amount=cost.amount,
                    currency=cost.unit or billing.currency,
                    resource_count=resource_counts.get(region, 0),
                    enabled=False,
                )
            )

        result["paid"].sort(key=lambda x: (-x.billing_amount, x.region))
        for key in ("used_unbilled", "enabled_unused", "billing_only"):
            result[key].sort(key=lambda x: x.region)

        return result

    @staticmethod
    def _components_for_names(names: Iterable[str], mapping):
        out = [component for name in names for component in mapping.get(name, [])]
        out.sort(key=lambda x: (-x.amount, x.usage_type))
        return out

    def _best_service_match(
        self,
        *,
        billing_name: str,
        discovered_services: Iterable[str],
    ):
        billing_norm = self._normalize(billing_name)
        aliases = self._dynamic_aliases(billing_name)
        best = None

        for service in discovered_services:
            norm = self._normalize(service)
            if not norm:
                continue

            if norm in aliases:
                candidate = (service, "dynamic_alias", 1.0)
            elif (
                len(norm) >= 3
                and (norm in billing_norm or billing_norm in norm)
            ):
                candidate = (service, "normalized_substring", 0.92)
            else:
                ratio = SequenceMatcher(None, norm, billing_norm).ratio()
                if ratio < 0.82:
                    continue
                candidate = (
                    service,
                    "string_similarity",
                    round(ratio, 4),
                )

            if best is None or candidate[2] > best[2]:
                best = candidate

        return best

    @classmethod
    def _dynamic_aliases(cls, value):
        tokens = cls._words(value)
        aliases = {cls._normalize(value)}
        for token in tokens:
            aliases.add(cls._normalize(token))
        for width in range(2, min(len(tokens), 7) + 1):
            for start in range(0, len(tokens) - width + 1):
                window = tokens[start : start + width]
                aliases.add(cls._normalize("".join(window)))
                aliases.add(cls._compress_initials(window))
        return {value for value in aliases if value}

    @staticmethod
    def _words(value):
        expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
        expanded = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", expanded)
        return re.findall(r"[A-Za-z]+\d*|\d+", expanded)

    @classmethod
    def _compress_initials(cls, words):
        initials = [
            cls._normalize(word)[0]
            for word in words
            if cls._normalize(word)
        ]
        out = []
        i = 0
        while i < len(initials):
            current = initials[i]
            j = i + 1
            while j < len(initials) and initials[j] == current:
                j += 1
            out.append(current)
            count = j - i
            if count > 1:
                out.append(str(count))
            i = j
        return "".join(out)

    @staticmethod
    def _normalize(value):
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    def _discover_relationships(
        self,
        resources: list[AWSResource],
    ) -> list[AWSResourceRelationship]:
        identifiers = defaultdict(list)
        for resource in resources:
            for identifier in (resource.arn, resource.resource_id):
                normalized = (identifier or "").strip().lower()
                if len(normalized) >= 6:
                    identifiers[normalized].append(resource)

        out = []
        seen = set()
        for source in resources:
            source_key = self._resource_key(source)
            if not source_key:
                continue

            for scalar in self._flatten_scalars(source.properties):
                for candidate in self._reference_candidates(scalar):
                    for target in identifiers.get(candidate.lower(), []):
                        target_key = self._resource_key(target)
                        if not target_key or target_key == source_key:
                            continue

                        pair = tuple(sorted((source_key, target_key)))
                        if pair in seen:
                            continue
                        seen.add(pair)

                        out.append(
                            AWSResourceRelationship(
                                source_arn=source.arn,
                                source_resource_id=source.resource_id,
                                source_service=source.service,
                                target_arn=target.arn,
                                target_resource_id=target.resource_id,
                                target_service=target.service,
                                evidence=candidate,
                            )
                        )
        return out

    @classmethod
    def _flatten_scalars(cls, value):
        out = []
        if isinstance(value, dict):
            for child in value.values():
                out.extend(cls._flatten_scalars(child))
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                out.extend(cls._flatten_scalars(child))
        elif value is not None:
            out.append(str(value))
        return out

    @staticmethod
    def _reference_candidates(value):
        return {
            match.rstrip(".,;:)")
            for match in re.findall(
                r'arn:[^\s"\',}\]]+|[A-Za-z0-9][A-Za-z0-9._:/-]{5,}',
                value,
            )
            if len(match.rstrip(".,;:)")) >= 6
        }

    @staticmethod
    def _resource_key(resource):
        if resource.arn:
            return resource.arn.lower()
        if resource.resource_id:
            return (
                f"{(resource.service or '').lower()}|"
                f"{(resource.region or '').lower()}|"
                f"{resource.resource_id.lower()}"
            )
        return None


