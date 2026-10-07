from rest_framework.permissions import AllowAny, IsAuthenticated





class ReadPublicWriteAuthenticated:
    """
    GET/HEAD/OPTIONS — open to everyone (customers, chatbot, anonymous).
    POST/PUT/PATCH/DELETE — staff JWT required.
    Mix this into any generics.ListCreateAPIView or
    RetrieveUpdateDestroyAPIView to make reads public
    while keeping writes protected.
    """

    def get_permissions(self):
        if self.request.method in ('GET', 'HEAD', 'OPTIONS'):
            return [AllowAny()]
        return [IsAuthenticated()]
    
