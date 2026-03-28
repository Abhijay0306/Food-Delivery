import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import api from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Package, ChevronRight, Clock } from "lucide-react";

const statusColors = {
  pending_payment: "bg-yellow-100 text-yellow-800",
  placed: "bg-blue-100 text-blue-800",
  accepted: "bg-indigo-100 text-indigo-800",
  preparing: "bg-amber-100 text-amber-800",
  ready: "bg-purple-100 text-purple-800",
  picked_up: "bg-teal-100 text-teal-800",
  delivered: "bg-green-100 text-green-800",
  rejected: "bg-red-100 text-red-800",
};

export default function CustomerOrders() {
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("all");

  useEffect(() => {
    const load = async () => {
      try {
        const params = filter === "active" ? { status: "active" } : {};
        const { data } = await api.get("/orders", { params });
        setOrders(data);
      } catch (e) { console.error(e); }
      setLoading(false);
    };
    load();
  }, [filter]);

  return (
    <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8" data-testid="customer-orders-page">
      <h1 className="font-display text-3xl tracking-tight text-stone-900 mb-2">My Orders</h1>
      <p className="text-sm text-stone-500 mb-6">Track and manage your food orders</p>

      {/* Filter */}
      <div className="flex gap-2 mb-6">
        {["all", "active"].map(f => (
          <button key={f} onClick={() => setFilter(f)}
            className={`px-4 py-1.5 rounded-full text-sm font-medium transition-colors ${filter === f ? "bg-orange-600 text-white" : "bg-stone-100 text-stone-600 hover:bg-stone-200"}`}
            data-testid={`filter-${f}`}>{f === "all" ? "All Orders" : "Active"}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="space-y-3">
          {[1,2,3].map(i => <div key={i} className="h-24 bg-stone-100 rounded-xl animate-pulse" />)}
        </div>
      ) : orders.length === 0 ? (
        <div className="text-center py-16">
          <Package className="h-12 w-12 text-stone-200 mx-auto mb-4" />
          <p className="text-stone-400 text-lg">No orders yet</p>
          <Link to="/browse" className="text-orange-600 text-sm hover:underline mt-2 inline-block" data-testid="browse-link">Browse kitchens</Link>
        </div>
      ) : (
        <div className="space-y-3" data-testid="orders-list">
          {orders.map(order => (
            <Link to={`/orders/${order.id}`} key={order.id}
              className="bg-white rounded-xl border border-stone-200/60 p-4 flex items-center gap-4 hover:shadow-md transition-shadow group"
              data-testid={`order-${order.id}`}>
              <div className="w-12 h-12 rounded-xl bg-orange-50 flex items-center justify-center shrink-0">
                <Package className="h-6 w-6 text-orange-600" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <h3 className="font-medium text-stone-900 text-sm">{order.kitchen_name}</h3>
                  <Badge className={`${statusColors[order.status] || "bg-stone-100 text-stone-800"} border-0 text-[10px]`}>
                    {order.status?.replace(/_/g, " ")}
                  </Badge>
                </div>
                <p className="text-xs text-stone-500">
                  {order.items?.length} items - ${order.total?.toFixed(2)}
                </p>
                <p className="text-xs text-stone-400 flex items-center gap-1 mt-1">
                  <Clock className="h-3 w-3" />
                  {order.created_at ? new Date(order.created_at).toLocaleDateString() : "-"}
                </p>
              </div>
              <ChevronRight className="h-5 w-5 text-stone-300 group-hover:text-orange-400 transition-colors shrink-0" />
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
