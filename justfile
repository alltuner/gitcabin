# ABOUTME: Task runner for the gitcabin site. site/ holds the finished page, so `just site`
# ABOUTME: only checks it is there.

site:
    test -f site/index.html
