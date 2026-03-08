from dataclasses import dataclass

#@dataclass
class SModUiListItem:
    
    def __init__(self, id = "", caption = "", isSelected = False, prefix ="", suffix = ""):
    # order important for construct calls! do not change!
        self.id: str = id
        self.caption: str = caption
        self.isSelected: bool = isSelected
        self.prefix: str = prefix
        self.suffix: str = suffix
        self.child_count: int = 0