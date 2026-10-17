"""Navigation endpoints for returning authorized navigation routes based on caller roles."""

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from src.api.deps import CurrentUser, get_current_user
from src.services.access_control.rbac import get_authorized_navigation_items

router = APIRouter(prefix="/navigation", tags=["navigation"])


class NavItemResponse(BaseModel):
    """Schema representing an individual navigation menu item."""

    title: str = Field(..., description="Display title for navigation section.")
    path: str = Field(..., description="Frontend route path.")
    is_accessible: bool = Field(..., description="Whether caller roles permit section access.")
    icon_id: str = Field(..., description="Icon identifier for frontend rendering.")


class NavigationMenuResponse(BaseModel):
    """Schema representing navigation menu endpoint response wrapper."""

    items: list[NavItemResponse] = Field(..., description="List of authorized navigation items.")


@router.get(
    "/menu",
    response_model=NavigationMenuResponse,
    status_code=status.HTTP_200_OK,
    summary="Get authorized navigation menu items",
    description="Returns global navigation routes marked with accessibility status for the authenticated user.",
)
@router.get(
    "",
    response_model=NavigationMenuResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
def get_navigation_menu(
    current_user: CurrentUser = Depends(get_current_user),
) -> NavigationMenuResponse:
    """Retrieve navigation routes and accessibility flags for active authenticated caller."""
    items = get_authorized_navigation_items(current_user.roles)
    return NavigationMenuResponse(
        items=[NavItemResponse(**item) for item in items]
    )
