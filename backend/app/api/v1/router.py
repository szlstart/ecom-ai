from fastapi import APIRouter

from app.modules.after_sale.admin_router import router as admin_after_sale_router
from app.modules.after_sale.router import router as after_sale_router
from app.modules.agent_runtime.router import router as agent_runtime_router
from app.modules.batch_jobs.router import router as batch_jobs_router
from app.modules.cart.router import router as cart_router
from app.modules.catalog.admin_router import router as admin_catalog_router
from app.modules.catalog.product_admin_router import router as product_admin_router
from app.modules.catalog.router import favorite_router
from app.modules.catalog.router import router as catalog_router
from app.modules.checkout.router import router as checkout_router
from app.modules.content.admin_router import router as admin_content_router
from app.modules.content.router import router as content_router
from app.modules.evaluation.router import observability_router
from app.modules.evaluation.router import router as evaluation_router
from app.modules.files.router import router as files_router
from app.modules.identity.router import auth_router, user_router
from app.modules.knowledge.router import ai_router
from app.modules.knowledge.router import router as knowledge_router
from app.modules.logistics.admin_router import router as admin_logistics_router
from app.modules.logistics.router import router as logistics_router
from app.modules.messaging.router import router as messaging_router
from app.modules.messaging.support_router import router as support_router
from app.modules.orders.router import router as orders_router
from app.modules.payments.router import router as payments_router
from app.modules.rbac.auth_router import router as admin_auth_router
from app.modules.rbac.router import router as admin_router
from app.modules.realtime.router import router as realtime_router
from app.modules.realtime.router import support_router as support_realtime_router
from app.modules.reviews.admin_router import router as admin_reviews_router
from app.modules.reviews.router import router as reviews_router
from app.modules.stores.admin_router import router as admin_store_router
from app.modules.stores.operations_router import router as store_operations_router
from app.modules.stores.router import follow_router
from app.modules.stores.router import router as stores_router

api_router = APIRouter()
api_router.include_router(agent_runtime_router)
api_router.include_router(auth_router)
api_router.include_router(user_router)
api_router.include_router(content_router)
api_router.include_router(admin_content_router)
api_router.include_router(evaluation_router)
api_router.include_router(observability_router)
api_router.include_router(files_router)
api_router.include_router(catalog_router)
api_router.include_router(reviews_router)
api_router.include_router(admin_reviews_router)
api_router.include_router(after_sale_router)
api_router.include_router(admin_after_sale_router)
api_router.include_router(messaging_router)
api_router.include_router(support_router)
api_router.include_router(realtime_router)
api_router.include_router(support_realtime_router)
api_router.include_router(cart_router)
api_router.include_router(checkout_router)
api_router.include_router(orders_router)
api_router.include_router(payments_router)
api_router.include_router(logistics_router)
api_router.include_router(knowledge_router)
api_router.include_router(ai_router)
api_router.include_router(admin_logistics_router)
api_router.include_router(favorite_router)
api_router.include_router(stores_router)
api_router.include_router(follow_router)
api_router.include_router(admin_catalog_router)
api_router.include_router(product_admin_router)
api_router.include_router(admin_store_router)
api_router.include_router(store_operations_router)
api_router.include_router(batch_jobs_router)
api_router.include_router(admin_auth_router)
api_router.include_router(admin_router)
