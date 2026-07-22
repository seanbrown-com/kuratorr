from django.db.models import F


def apply_sorting(request, queryset, allowed, default):
    """Apply a whitelisted sort and build header URLs that retain current filters."""
    sort = request.GET.get("sort", default)
    if sort not in allowed:
        sort = default
    direction = request.GET.get("direction", "asc")
    if direction not in {"asc", "desc"}:
        direction = "asc"

    fields = allowed[sort]
    if not isinstance(fields, (list, tuple)):
        fields = (fields,)
    ordering = []
    for field in fields:
        expression = F(field) if isinstance(field, str) else field
        ordering.append(
            expression.desc(nulls_last=True)
            if direction == "desc"
            else expression.asc(nulls_last=True)
        )
    ordering.append(F("pk").asc())
    queryset = queryset.order_by(*ordering)

    headers = {}
    for key in allowed:
        params = request.GET.copy()
        params.pop("page", None)
        params["sort"] = key
        params["direction"] = "desc" if key == sort and direction == "asc" else "asc"
        headers[key] = {
            "url": f"?{params.urlencode()}",
            "active": key == sort,
            "indicator": "▲" if key == sort and direction == "asc" else "▼",
        }

    page_params = request.GET.copy()
    page_params.pop("page", None)
    page_params["sort"] = sort
    page_params["direction"] = direction
    return queryset, {
        "sort": sort,
        "sort_direction": direction,
        "sort_headers": headers,
        "page_query": page_params.urlencode(),
    }
