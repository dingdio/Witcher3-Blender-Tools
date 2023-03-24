import numpy as np

class W3LanguageKey(object):
    def __init__(self, value:np.uint32):
        self.value = value
    def __str__(self):
        return str(self.value)

class W3LanguageMagic(object):
    def __init__(self, value:np.uint32):
        self.value = value
    def __str__(self):
        return str(self.value)

class W3Language(object):
    
    def __init__(self, key:W3LanguageKey, magic:W3LanguageMagic, handle:str):
        self.Key = key
        self.Magic = magic
        self.Handle = handle
    def __str__(self):
        return f"W3Language({str(self.Key)},{str(self.Magic)},{self.Handle})"
    
languages = [
    W3Language(W3LanguageKey(0x00000000), W3LanguageMagic(0x00000000), "ar"),
    W3Language(W3LanguageKey(0x00000000), W3LanguageMagic(0x00000000), "br"),
    W3Language(W3LanguageKey(0x00000000), W3LanguageMagic(0x00000000), "esMX"),
    W3Language(W3LanguageKey(0x00000000), W3LanguageMagic(0x00000000), "kr"),
    W3Language(W3LanguageKey(0x00000000), W3LanguageMagic(0x00000000), "tr"),
    W3Language(W3LanguageKey(0x83496237), W3LanguageMagic(0x73946816), "pl"),
    W3Language(W3LanguageKey(0x43975139), W3LanguageMagic(0x79321793), "en"),
    W3Language(W3LanguageKey(0x75886138), W3LanguageMagic(0x42791159), "de"),
    W3Language(W3LanguageKey(0x45931894), W3LanguageMagic(0x12375973), "it"),
    W3Language(W3LanguageKey(0x23863176), W3LanguageMagic(0x75921975), "fr"),
    W3Language(W3LanguageKey(0x24987354), W3LanguageMagic(0x21793217), "cz"),
    W3Language(W3LanguageKey(0x18796651), W3LanguageMagic(0x42387566), "es"),
    W3Language(W3LanguageKey(0x18632176), W3LanguageMagic(0x16875467), "zh"),
    W3Language(W3LanguageKey(0x63481486), W3LanguageMagic(0x42386347), "ru"),
    W3Language(W3LanguageKey(0x42378932), W3LanguageMagic(0x67823218), "hu"),
    W3Language(W3LanguageKey(0x54834893), W3LanguageMagic(0x59825646), "jp")
]