"""External-service clients (Mojang, Fabric, Adoptium, Modrinth, …).

Each provider keeps network I/O separate from pure parsing so the parsing is
unit-testable offline against recorded fixtures.
"""
