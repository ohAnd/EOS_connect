"""
Forecast metadata: which tail of a served array is invention, and of what kind.

PriceInterface grew these three fields ad hoc; the feed-in interface is the
second producer and later domains (temperature, base load) will be the third
and fourth, so the triple lives in one mixin instead of another copy of the
same three attributes and two accessors. PriceInterface overrides the two
accessors to read and clear its published price snapshot alongside.
"""

import logging

logger = logging.getLogger("__main__")


class ForecastMetadataMixin:
    """
    The forecast_start_index / forecast_type / forecast_source triple.

    Call ``_init_forecast_metadata()`` in the consumer's ``__init__``.
    ``forecast_type`` vocabulary: "all_real", "simple_repetition",
    "smart_forecast", or None while nothing has been served yet.
    """

    def _init_forecast_metadata(self):
        self.forecast_start_index = None
        self.forecast_type = None
        self.forecast_source = None

    def _set_forecast_metadata(self, start_index, forecast_type, source=None):
        """Record where invention starts in the currently served array."""
        self.forecast_start_index = start_index
        self.forecast_type = forecast_type
        self.forecast_source = source
        if forecast_type and forecast_type != "all_real":
            logger.debug(
                "[%s] Forecast metadata updated: type=%s, start_index=%s, source=%s",
                type(self).__name__,
                forecast_type,
                start_index,
                source,
            )

    def get_forecast_metadata(self):
        """The triple, in the shape the web API serves."""
        return {
            "forecast_start_index": self.forecast_start_index,
            "forecast_type": self.forecast_type,
            "forecast_source": self.forecast_source,
        }

    def clear_forecast_metadata(self):
        """Drop markers belonging to a previous source."""
        self._set_forecast_metadata(None, None, None)
