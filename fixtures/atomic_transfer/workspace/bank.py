def transfer(accounts, source, target, amount):
    accounts[source] -= amount
    if accounts[source] < 0:
        raise ValueError("insufficient funds")
    accounts[target] += amount
