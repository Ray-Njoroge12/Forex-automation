"""Forward-test protocol tools (validation wave 4, R10).

freeze: record and check the frozen configuration a forward test runs on.
prereg: seal a versioned go/no-go and abort criteria file before the start.
evaluate: score recorded demo trades against sealed criteria (read-only DB).

None of these tools place, size or route orders, write to the bridge folder,
or change a policy constant.
"""
