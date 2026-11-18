def page(rows, offset, limit):
    items = rows[offset:offset + limit]
    return {"items": items, "total": len(items)}
