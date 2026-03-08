class MQuaternion:
    def __init__(self, x, y, z, w):
            self.X = x
            self.Y = y
            self.Z = z
            self.W = w
    def normalizeIt(self):
        pass
    def asEulerRotation(self):
        return self
    def invertIt(self):
        self.X = -self.X
        self.Y = -self.Y
        self.Z = -self.Z
        #self.W = -self.W
    