from names import slug

def page_path(title):
    return "/" + slug(title)
