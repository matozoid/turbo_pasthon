# Makes the git repositories the programs use: greet (v1 says "Hello", v2 "Hi"), and the
# packages hello (using greet v1) and hey (using greet v2).
set -e
here=$PWD
commit() { git -c user.name=t -c user.email=t@t commit -q -m "$1"; }
printf 'unit HelloDeps\n\nconst\n    export Greet = "%s/greet@v1"\n' "$here" > hello/HelloDeps.pst
sed -i "s|REPOS|$here|" hey/Hey.pst
for repo in greet hello hey; do
    (cd $repo && git -c init.defaultBranch=main init -q && git add -A && commit one && git tag v1)
done
(cd greet && sed -i 's/Hello/Hi/' GreetText.pst && git add -A && commit two && git tag v2 && git rev-parse HEAD~1 > ../first)
printf 'unit Deps\n\nconst\n    export Greet = "%s/greet@v1"\n    export Hello = "%s/hello@v1"\n    export Hey = "file://%s/hey@v1"\n    export Bad = 3\n    Hidden = "x@v1"\n' "$here" "$here" "$here" > Deps.pst
printf 'unit Commit\n\nconst\n    export Greet = "%s/greet@%s"\n' "$here" "$(cat first)" > Commit.pst
