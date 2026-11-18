def retry_delay(attempt, maximum):
    return min(2 ** attempt, maximum)
